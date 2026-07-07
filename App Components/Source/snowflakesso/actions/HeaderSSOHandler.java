package snowflakesso.actions;

import com.mendix.core.Core;
import com.mendix.core.CoreException;
import com.mendix.externalinterface.connector.RequestHandler;
import com.mendix.m2ee.api.IMxRuntimeRequest;
import com.mendix.m2ee.api.IMxRuntimeResponse;
import com.mendix.systemwideinterfaces.core.IContext;
import com.mendix.systemwideinterfaces.core.IMendixIdentifier;
import com.mendix.systemwideinterfaces.core.IMendixObject;
import com.mendix.systemwideinterfaces.core.ISession;
import com.mendix.systemwideinterfaces.core.IUser;

import com.mendix.core.objectmanagement.member.MendixObjectReferenceSet;

import net.snowflake.client.jdbc.internal.fasterxml.jackson.databind.ObjectMapper;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.UUID;

public class HeaderSSOHandler extends RequestHandler {

    private static final String HEADER_SF_USER = "Sf-Context-Current-User";
    private static final String HEADER_SF_TOKEN = "Sf-Context-Current-User-Token";
    private static final String LOG_NODE = "SnowflakeSSO";

    // Configurable: the Mendix user role to assign to auto-provisioned users.
    // Set this via a Mendix constant or hardcode for POC.
    private static final String DEFAULT_USER_ROLE = "User";

    // Operator-set map of Snowflake account role (uppercase) -> Mendix userrole,
    // delivered by the controller as plain (non-secret) JSON. Absent or malformed
    // means "role mapping is off" - end-users keep the static DEFAULT_USER_ROLE.
    private static final String ENV_ROLE_MAPPING = "MX_ROLE_MAPPING";
    private static final String SERVICE_TOKEN_PATH = "/snowflake/session/token";
    private static final int JDBC_LOGIN_TIMEOUT_SECS = 10;

    // Parsed once: the env var is immutable for the container's lifetime, and a
    // login-time JSON parse is wasted work on every request.
    private static volatile Map<String, String> roleMappingCache = null;

    @Override
    protected void processRequest(
            IMxRuntimeRequest request,
            IMxRuntimeResponse response,
            String path) throws Exception {

        HttpServletRequest httpReq = request.getHttpServletRequest();
        HttpServletResponse httpResp = response.getHttpServletResponse();

        // Handle token refresh endpoint (lightweight, no session creation)
        if (path != null && path.startsWith("refresh")) {
            handleTokenRefresh(request, response, httpReq);
            return;
        }

        // 1. Read the trusted identity header
        String snowflakeUsername = httpReq.getHeader(HEADER_SF_USER);

        if (snowflakeUsername == null || snowflakeUsername.isBlank()) {
            Core.getLogger(LOG_NODE).error(
                "No " + HEADER_SF_USER + " header found. "
                + "This handler must only be accessed through the SPCS ingress proxy.");
            response.setStatus(IMxRuntimeResponse.UNAUTHORIZED);
            httpResp.setContentType("text/plain");
            httpResp.getWriter().write("Authentication required.");
            return;
        }

        Core.getLogger(LOG_NODE).info("SSO login for Snowflake user: " + snowflakeUsername);

        IContext systemContext = Core.createSystemContext();

        // 2. Find or create the Mendix user as SnowflakeUser
        IUser user = Core.getUser(systemContext, snowflakeUsername);

        if (user == null) {
            user = provisionUser(systemContext, snowflakeUsername);
            Core.getLogger(LOG_NODE).info("Auto-provisioned new user: " + snowflakeUsername);
        } else {
            // Check if the existing user is a SnowflakeUser specialization.
            // If not (e.g. created as plain System.User before), delete and re-create.
            IMendixObject existingObj = user.getMendixObject();
            if (!existingObj.isInstanceOf("SnowflakeSSO.SnowflakeUser")) {
                Core.getLogger(LOG_NODE).warn(
                    "User " + snowflakeUsername + " exists as " + existingObj.getType()
                    + ", upgrading to SnowflakeSSO.SnowflakeUser");
                Core.delete(systemContext, existingObj);
                user = provisionUser(systemContext, snowflakeUsername);
            }
        }

        // 3. Sync Mendix userroles from the caller's Snowflake account roles, if role
        // mapping is configured. This MUST happen before Core.initializeSession: Mendix
        // fixes the session's role set at session init, so a sync applied after would
        // only take effect at the user's next login.
        String callerToken = httpReq.getHeader(HEADER_SF_TOKEN);
        syncUserRoles(systemContext, user, callerToken);

        // 4. Initialize a session (bypasses password check)
        ISession session = Core.initializeSession(user, null);

        if (session == null) {
            Core.getLogger(LOG_NODE).error("Failed to initialize session for: " + snowflakeUsername);
            response.setStatus(IMxRuntimeResponse.INTERNAL_SERVER_ERROR);
            httpResp.getWriter().write("Session initialization failed.");
            return;
        }

        // 5. Store the caller token on the user object
        if (callerToken != null && !callerToken.isBlank()) {
            IMendixObject userObj = user.getMendixObject();
            userObj.setValue(systemContext, "CallerToken", callerToken);
            Core.commit(systemContext, userObj);
            Core.getLogger(LOG_NODE).debug("Stored caller token for: " + snowflakeUsername);
        }

        // 6. Let Mendix set all session cookies (XASSESSIONID, CSRF token, flags)
        Core.addMendixCookies(request, response, session, false);

        // 7. Redirect to the original destination or home
        String cont = httpReq.getParameter("cont");
        String redirectUrl = "/index.html";
        if (cont != null && !cont.isBlank()) {
            redirectUrl = URLDecoder.decode(cont, StandardCharsets.UTF_8);
            // Safety: only allow relative redirects
            if (redirectUrl.startsWith("http") || redirectUrl.startsWith("//")) {
                redirectUrl = "/index.html";
            }
        }

        response.setStatus(IMxRuntimeResponse.SEE_OTHER);
        response.addHeader("Location", redirectUrl);

        Core.getLogger(LOG_NODE).debug(
            "Session created for " + snowflakeUsername + ", redirecting to " + redirectUrl);
    }

    /**
     * Lightweight endpoint for refreshing the caller token.
     * Called periodically by the client JS action via /headersso/refresh.
     * Resolves the user from the existing Mendix session cookie.
     */
    private void handleTokenRefresh(
            IMxRuntimeRequest request,
            IMxRuntimeResponse response,
            HttpServletRequest httpReq) throws Exception {

        String callerToken = httpReq.getHeader(HEADER_SF_TOKEN);
        if (callerToken == null || callerToken.isBlank()) {
            Core.getLogger(LOG_NODE).warn("Token refresh called but no " + HEADER_SF_TOKEN + " header present.");
            response.setStatus(IMxRuntimeResponse.UNAUTHORIZED);
            return;
        }

        ISession session = this.getSessionFromRequest(request);
        if (session == null) {
            Core.getLogger(LOG_NODE).warn("Token refresh called but no active session found.");
            response.setStatus(IMxRuntimeResponse.UNAUTHORIZED);
            return;
        }

        IContext systemContext = Core.createSystemContext();
        IUser user = session.getUser(systemContext);
        if (user == null) {
            response.setStatus(IMxRuntimeResponse.UNAUTHORIZED);
            return;
        }

        IMendixObject userObj = user.getMendixObject();
        userObj.setValue(systemContext, "CallerToken", callerToken);
        Core.commit(systemContext, userObj);

        Core.getLogger(LOG_NODE).debug("Refreshed caller token for: " + user.getName());
        response.setStatus(204);
    }

    /**
     * Creates a new Mendix user (SnowflakeSSO.SnowflakeUser) for the given Snowflake username.
     * Assigns the configured default role.
     */
    private IUser provisionUser(IContext systemContext, String username) throws CoreException {
        IMendixObject accountObj = Core.instantiate(systemContext, "SnowflakeSSO.SnowflakeUser");
        accountObj.setValue(systemContext, "Name", username);
        // Set a random unusable password (login is header-based only)
        accountObj.setValue(systemContext, "Password", "SSO_" + UUID.randomUUID());

        // Assign user role via reference set add (safe, does not overwrite)
        List<IMendixObject> roles = Core.createXPathQuery(
            String.format("//System.UserRole[Name='%s']", DEFAULT_USER_ROLE))
            .execute(systemContext);

        if (!roles.isEmpty()) {
            MendixObjectReferenceSet userRoles =
                (MendixObjectReferenceSet) accountObj.getMember("System.UserRoles");
            userRoles.addValue(systemContext, roles.get(0).getId());
        }

        Core.commit(systemContext, accountObj);
        return Core.getUser(systemContext, username);
    }

    /**
     * Lazily parses MX_ROLE_MAPPING (Snowflake account role -> Mendix userrole) once
     * per container lifetime. Keys are uppercased defensively (the controller already
     * uppercases them; Snowflake role names are case-insensitive identifiers).
     * Returns an empty map when the env var is absent or malformed - callers must
     * treat that as "role mapping is off", never as an error.
     */
    private static Map<String, String> loadRoleMapping() {
        Map<String, String> cached = roleMappingCache;
        if (cached != null) {
            return cached;
        }
        synchronized (HeaderSSOHandler.class) {
            if (roleMappingCache != null) {
                return roleMappingCache;
            }
            Map<String, String> result = new HashMap<>();
            String raw = System.getenv(ENV_ROLE_MAPPING);
            if (raw != null && !raw.isBlank()) {
                try {
                    ObjectMapper mapper = new ObjectMapper();
                    @SuppressWarnings("unchecked")
                    Map<String, String> parsed = mapper.readValue(raw, Map.class);
                    for (Map.Entry<String, String> entry : parsed.entrySet()) {
                        result.put(entry.getKey().toUpperCase(), entry.getValue());
                    }
                } catch (Exception e) {
                    Core.getLogger(LOG_NODE).error(
                        "Failed to parse " + ENV_ROLE_MAPPING + " as JSON; role mapping is disabled: "
                        + e.getMessage());
                }
            }
            roleMappingCache = result;
            return roleMappingCache;
        }
    }

    /**
     * Opens a compound-token Snowflake session as the caller and returns their
     * account roles (uppercased). Mirrors the recipe in Admin UI/app/auth.py.
     */
    private List<String> fetchAvailableRoles(String callerToken) throws Exception {
        String serviceToken = new String(
            Files.readAllBytes(Paths.get(SERVICE_TOKEN_PATH)), StandardCharsets.UTF_8).trim();
        String compound = serviceToken + "." + callerToken;
        String url = "jdbc:snowflake://" + System.getenv("SNOWFLAKE_HOST") + "/";

        Properties props = new Properties();
        props.put("authenticator", "oauth");
        props.put("token", compound);
        props.put("loginTimeout", JDBC_LOGIN_TIMEOUT_SECS);
        props.put("networkTimeout", 10000);

        // Force-register the driver: DriverManager's ServiceLoader-based
        // auto-discovery depends on the calling thread's context classloader,
        // which a Mendix Java action's userlib jar does not reliably set,
        // causing "No suitable driver" even though the jar is on the classpath.
        Class.forName("net.snowflake.client.api.driver.SnowflakeDriver");

        // No warehouse: CURRENT_AVAILABLE_ROLES() is a session function, run
        // warehouse-less the same way the Admin UI runs it in production.
        try (Connection conn = DriverManager.getConnection(url, props);
             Statement stmt = conn.createStatement();
             ResultSet rs = stmt.executeQuery("SELECT CURRENT_AVAILABLE_ROLES()")) {
            List<String> roles = new java.util.ArrayList<>();
            if (rs.next()) {
                String json = rs.getString(1);
                if (json != null) {
                    ObjectMapper mapper = new ObjectMapper();
                    @SuppressWarnings("unchecked")
                    List<String> raw = mapper.readValue(json, List.class);
                    for (String r : raw) {
                        roles.add(r.toUpperCase());
                    }
                }
            }
            return roles;
        }
    }

    /**
     * Syncs the user's Mendix userroles from their Snowflake account roles, per
     * MX_ROLE_MAPPING. Never throws and never blocks login: any failure just leaves
     * the user's existing/default role untouched.
     */
    private void syncUserRoles(IContext systemContext, IUser user, String callerToken) {
        Map<String, String> mapping = loadRoleMapping();
        if (mapping.isEmpty()) {
            return; // legacy static behavior: no mapping configured
        }
        if (callerToken == null || callerToken.isBlank()) {
            Core.getLogger(LOG_NODE).info(
                "Role mapping is configured but no caller token is present for "
                + user.getName() + " (is use_caller_rights enabled?); keeping existing/default role.");
            return;
        }

        List<String> availableRoles;
        try {
            availableRoles = fetchAvailableRoles(callerToken);
        } catch (Exception e) {
            Core.getLogger(LOG_NODE).warn(
                "Role sync failed for " + user.getName() + " (Snowflake unreachable?); "
                + "falling back to existing/default role: " + e.getMessage());
            return;
        }

        Set<String> availableSet = new HashSet<>(availableRoles);
        Set<String> target = new HashSet<>();
        for (Map.Entry<String, String> entry : mapping.entrySet()) {
            if (availableSet.contains(entry.getKey())) {
                target.add(entry.getValue());
            }
        }
        if (target.isEmpty()) {
            Core.getLogger(LOG_NODE).info(
                "User " + user.getName() + " holds no Snowflake role mapped to a Mendix userrole; "
                + "falling back to default role " + DEFAULT_USER_ROLE);
            target.add(DEFAULT_USER_ROLE);
        }

        // Managed subset: only roles this mapping (plus the default) is allowed to
        // touch, so roles granted inside the app but not managed here are never
        // clobbered.
        Set<String> managed = new HashSet<>(mapping.values());
        managed.add(DEFAULT_USER_ROLE);

        try {
            // Load every System.UserRole once (no per-name XPath, no quoting concern -
            // mapping values with quotes are already rejected by the controller).
            List<IMendixObject> allRoles = Core.createXPathQuery("//System.UserRole").execute(systemContext);
            Map<String, IMendixObject> roleByName = new HashMap<>();
            Map<IMendixIdentifier, String> nameById = new HashMap<>();
            for (IMendixObject roleObj : allRoles) {
                String name = (String) roleObj.getValue(systemContext, "Name");
                if (name != null) {
                    roleByName.put(name, roleObj);
                    nameById.put(roleObj.getId(), name);
                }
            }

            IMendixObject userObj = user.getMendixObject();
            MendixObjectReferenceSet userRolesMember =
                (MendixObjectReferenceSet) userObj.getMember("System.UserRoles");
            List<IMendixIdentifier> currentIds = userRolesMember.getValue(systemContext);

            Set<String> currentNames = new HashSet<>();
            boolean changed = false;
            for (IMendixIdentifier id : currentIds) {
                String name = nameById.get(id);
                if (name == null) {
                    continue;
                }
                currentNames.add(name);
                if (managed.contains(name) && !target.contains(name)) {
                    userRolesMember.removeValue(systemContext, id);
                    changed = true;
                }
            }

            for (String roleName : target) {
                if (currentNames.contains(roleName)) {
                    continue;
                }
                IMendixObject roleObj = roleByName.get(roleName);
                if (roleObj == null) {
                    Core.getLogger(LOG_NODE).warn(
                        "Role mapping targets Mendix userrole '" + roleName + "' which does not exist; skipping.");
                    continue;
                }
                userRolesMember.addValue(systemContext, roleObj.getId());
                changed = true;
            }

            if (changed) {
                Core.commit(systemContext, userObj);
            }
            Core.getLogger(LOG_NODE).info(
                "Role sync succeeded for " + user.getName() + "; userroles: " + target
                + (changed ? " (updated)" : " (unchanged)"));
        } catch (CoreException e) {
            Core.getLogger(LOG_NODE).warn(
                "Role sync failed to apply for " + user.getName() + "; "
                + "falling back to existing/default role: " + e.getMessage());
        }
    }
}
