package snowflakesso.actions;

import com.mendix.core.Core;
import com.mendix.core.CoreException;
import com.mendix.externalinterface.connector.RequestHandler;
import com.mendix.m2ee.api.IMxRuntimeRequest;
import com.mendix.m2ee.api.IMxRuntimeResponse;
import com.mendix.systemwideinterfaces.core.IContext;
import com.mendix.systemwideinterfaces.core.IMendixObject;
import com.mendix.systemwideinterfaces.core.ISession;
import com.mendix.systemwideinterfaces.core.IUser;

import com.mendix.core.objectmanagement.member.MendixObjectReferenceSet;

import snowflakesso.CallerTokenCache;

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
import java.util.ArrayList;
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

        // 1b. The header alone is not proof of identity - it is only trustworthy
        // because SPCS ingress is assumed not to forward it from anywhere but the
        // authenticated edge. Whenever a caller token is present, verify it actually
        // authenticates as the claimed username before trusting the header at all;
        // this closes the gap for any path where that ingress assumption doesn't
        // hold (e.g. a co-resident container reaching this service directly).
        String callerToken = httpReq.getHeader(HEADER_SF_TOKEN);
        if (callerToken != null && !callerToken.isBlank()
                && !verifyCallerIdentity(snowflakeUsername, callerToken)) {
            response.setStatus(IMxRuntimeResponse.UNAUTHORIZED);
            httpResp.setContentType("text/plain");
            httpResp.getWriter().write("Caller token identity mismatch.");
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
        syncUserRoles(systemContext, user, callerToken);

        // 4. Initialize a session (bypasses password check)
        ISession session = Core.initializeSession(user, null);

        if (session == null) {
            Core.getLogger(LOG_NODE).error("Failed to initialize session for: " + snowflakeUsername);
            response.setStatus(IMxRuntimeResponse.INTERNAL_SERVER_ERROR);
            httpResp.getWriter().write("Session initialization failed.");
            return;
        }

        // 5. Cache the caller token in memory (never persisted - see S1b / CallerTokenCache)
        if (callerToken != null && !callerToken.isBlank()) {
            CallerTokenCache.put(snowflakeUsername, callerToken);
            Core.getLogger(LOG_NODE).debug("Cached caller token for: " + snowflakeUsername);
        }

        // 6. Let Mendix set all session cookies (XASSESSIONID, CSRF token, flags)
        Core.addMendixCookies(request, response, session, false);

        // 7. Redirect to the original destination or home
        String cont = httpReq.getParameter("cont");
        String redirectUrl = "/index.html";
        if (cont != null && !cont.isBlank()) {
            String decodedCont = URLDecoder.decode(cont, StandardCharsets.UTF_8);
            // Safety: allow-list only. A prior deny-list here (rejecting only
            // startsWith("http")/startsWith("//")) was bypassable via case variants
            // (HTTP://evil.com), backslash forms (/\evil.com), and leading
            // whitespace/control characters. Only accept a value that is
            // unambiguously a same-origin path: exactly one leading '/', a second
            // character that isn't '/' or '\' (both of which browsers resolve as a
            // scheme-relative absolute URL), and no backslash or control character
            // anywhere in the value.
            if (isSafeRedirectPath(decodedCont)) {
                redirectUrl = decodedCont;
            }
        }

        response.setStatus(IMxRuntimeResponse.SEE_OTHER);
        response.addHeader("Location", redirectUrl);

        Core.getLogger(LOG_NODE).debug(
            "Session created for " + snowflakeUsername + ", redirecting to " + redirectUrl);
    }

    /**
     * Allow-list check for the "cont" redirect target: true only for a value that is
     * unambiguously a same-origin relative path, never something a browser could
     * resolve as an absolute or scheme-relative URL.
     */
    private static boolean isSafeRedirectPath(String path) {
        if (path.length() < 2 || path.charAt(0) != '/') {
            return false;
        }
        char second = path.charAt(1);
        if (second == '/' || second == '\\') {
            return false;
        }
        // Reject a backslash (browsers treat it as '/') or any control character
        // (leading control chars are stripped before URL parsing, so " \t//evil"
        // could resolve scheme-relative). A leading single '/' with a non-slash
        // second char is already an unambiguous same-origin path, so no scheme
        // check is needed and legitimate paths like "/httpservice" stay allowed.
        for (int i = 0; i < path.length(); i++) {
            char c = path.charAt(i);
            if (c == '\\' || c < 0x20 || c == 0x7f) {
                return false;
            }
        }
        return true;
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

        CallerTokenCache.put(user.getName(), callerToken);
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
     * Opens a compound-token Snowflake session as the caller. Shared by role-sync
     * (fetchAvailableRoles) and caller-token identity verification
     * (verifyCallerIdentity) - both need the identical OAuth connection recipe.
     * Mirrors the recipe in Admin UI/app/auth.py.
     */
    private Connection openCallerSession(String callerToken) throws Exception {
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

        return DriverManager.getConnection(url, props);
    }

    /**
     * Returns the caller's account roles (uppercased), via a caller-rights session.
     */
    private List<String> fetchAvailableRoles(String callerToken) throws Exception {
        // No warehouse: CURRENT_AVAILABLE_ROLES() is a session function, run
        // warehouse-less the same way the Admin UI runs it in production.
        try (Connection conn = openCallerSession(callerToken);
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
     * Verifies that callerToken actually authenticates as claimedUsername, by
     * opening a real caller-rights Snowflake session and checking CURRENT_USER().
     * See the call site's comment for why this check exists.
     *
     * Returns true only on a confirmed match, or when verification could not be
     * completed at all (Snowflake unreachable, JDBC failure) - an inconclusive
     * result does not block login, the same fail-soft philosophy syncUserRoles
     * already uses for role sync below. Returns false only on a definite,
     * confirmed mismatch, which is the one case this check exists to catch.
     */
    private boolean verifyCallerIdentity(String claimedUsername, String callerToken) {
        try (Connection conn = openCallerSession(callerToken);
             Statement stmt = conn.createStatement();
             ResultSet rs = stmt.executeQuery("SELECT CURRENT_USER()")) {
            if (!rs.next()) {
                Core.getLogger(LOG_NODE).warn(
                    "Caller token verification returned no row for " + claimedUsername + "; proceeding.");
                return true;
            }
            String actual = rs.getString(1);
            boolean matches = actual != null && actual.equalsIgnoreCase(claimedUsername);
            if (!matches) {
                Core.getLogger(LOG_NODE).error(
                    "Caller token identity mismatch: header claimed '" + claimedUsername
                    + "' but the token authenticates as '" + actual + "'. Rejecting.");
            }
            return matches;
        } catch (Exception e) {
            Core.getLogger(LOG_NODE).warn(
                "Could not verify caller token identity for " + claimedUsername
                + " (Snowflake unreachable?); proceeding on header trust: " + e.getMessage());
            return true;
        }
    }

    /**
     * Syncs the user's Mendix userroles from their Snowflake account roles, per
     * MX_ROLE_MAPPING. Never throws and never blocks login: any failure to reach
     * Snowflake or to read the caller token leaves the user's existing role
     * untouched. An empty/absent mapping is treated differently from a failure -
     * it's a deliberate "sync is off" state, so any role a prior sync granted
     * (tracked via the SnowflakeUser_LastSyncedRoles reference set) is actively
     * reverted to DEFAULT_USER_ROLE rather than left in place.
     */
    private void syncUserRoles(IContext systemContext, IUser user, String callerToken) {
        Map<String, String> mapping = loadRoleMapping();

        Set<String> target = new HashSet<>();
        if (mapping.isEmpty()) {
            target.add(DEFAULT_USER_ROLE);
        } else {
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
        }

        try {
            snowflakesso.proxies.SnowflakeUser sfUser =
                snowflakesso.proxies.SnowflakeUser.initialize(systemContext, user.getMendixObject());

            Map<String, system.proxies.UserRole> roleByName = new HashMap<>();
            for (system.proxies.UserRole role : system.proxies.UserRole.load(systemContext, "")) {
                roleByName.put(role.getName(systemContext), role);
            }

            // Resolve every target role name once, up front, so a role that no
            // longer exists is warned about exactly once rather than at each of
            // its two use sites below.
            Map<String, system.proxies.UserRole> targetResolved = new HashMap<>();
            for (String roleName : target) {
                system.proxies.UserRole roleObj = roleByName.get(roleName);
                if (roleObj == null) {
                    Core.getLogger(LOG_NODE).warn(
                        "Role mapping targets Mendix userrole '" + roleName + "' which does not exist; skipping.");
                    continue;
                }
                targetResolved.put(roleName, roleObj);
            }

            List<system.proxies.UserRole> currentRoles = sfUser.getUserRoles(systemContext);
            List<system.proxies.UserRole> lastSyncedRoles = sfUser.getSnowflakeUser_LastSyncedRoles(systemContext);

            Set<String> currentNames = new HashSet<>();
            for (system.proxies.UserRole role : currentRoles) {
                currentNames.add(role.getName(systemContext));
            }
            Set<String> lastSyncedNames = new HashSet<>();
            for (system.proxies.UserRole role : lastSyncedRoles) {
                lastSyncedNames.add(role.getName(systemContext));
            }

            // Managed subset: roles the CURRENT mapping targets, plus roles a PRIOR
            // sync granted, plus the default. Including the prior-sync set is what
            // lets a role be reverted after its mapping entry (or the whole mapping)
            // is removed, while a role granted manually inside the app - never
            // reflected in either set - is left untouched.
            Set<String> managed = new HashSet<>(mapping.values());
            managed.addAll(lastSyncedNames);
            managed.add(DEFAULT_USER_ROLE);

            List<system.proxies.UserRole> newUserRoles = new ArrayList<>();
            Set<String> newNames = new HashSet<>();
            for (system.proxies.UserRole role : currentRoles) {
                String name = role.getName(systemContext);
                if (!managed.contains(name)) {
                    newUserRoles.add(role);
                    newNames.add(name);
                }
            }
            for (Map.Entry<String, system.proxies.UserRole> entry : targetResolved.entrySet()) {
                if (newNames.add(entry.getKey())) {
                    newUserRoles.add(entry.getValue());
                }
            }

            boolean userRolesChanged = !newNames.equals(currentNames);
            boolean lastSyncedChanged = !targetResolved.keySet().equals(lastSyncedNames);

            if (userRolesChanged) {
                sfUser.setUserRoles(systemContext, newUserRoles);
            }
            if (lastSyncedChanged) {
                sfUser.setSnowflakeUser_LastSyncedRoles(systemContext, new ArrayList<>(targetResolved.values()));
            }
            if (userRolesChanged || lastSyncedChanged) {
                Core.commit(systemContext, sfUser.getMendixObject());
            }

            Core.getLogger(LOG_NODE).info(
                "Role sync succeeded for " + user.getName() + "; userroles: " + target
                + (userRolesChanged ? " (updated)" : " (unchanged)"));
        } catch (CoreException e) {
            Core.getLogger(LOG_NODE).warn(
                "Role sync failed to apply for " + user.getName() + "; "
                + "falling back to existing/default role: " + e.getMessage());
        }
    }
}
