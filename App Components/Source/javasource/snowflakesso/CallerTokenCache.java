package snowflakesso;

import java.util.concurrent.ConcurrentHashMap;

/**
 * In-memory, per-container cache of each user's live Snowflake caller OAuth
 * token, keyed by Mendix username. Replaces persisting the raw token to the
 * SnowflakeUser.CallerToken column in Postgres: a live ~30-minute credential
 * sitting in plaintext at rest is a real exposure even after per-app database
 * isolation (see the review's S1b finding). HeaderSSOHandler populates this on
 * login and on each periodic refresh; GetCompoundToken reads from here instead
 * of the database.
 *
 * TTL is set comfortably under the app's configured
 * SERVICE_CALLER_TOKEN_VALIDITY_SECS (1800s / 30 min in this app's setup
 * script) and above RefreshCallerToken.js's 20-minute refresh cadence, so a
 * missed refresh tick still has margin before the entry is treated as stale.
 *
 * Caveat: this cache is per-JVM/per-container. A container restart clears it
 * (the user simply logs in again - not a correctness issue), and it is NOT
 * shared across replicas if this service's compute pool ever runs more than
 * one instance for a single app (it doesn't today).
 */
public final class CallerTokenCache {

    private static final long TTL_MILLIS = 25 * 60 * 1000;

    private static final class Entry {
        final String token;
        final long storedAtMillis;

        Entry(String token, long storedAtMillis) {
            this.token = token;
            this.storedAtMillis = storedAtMillis;
        }
    }

    private static final ConcurrentHashMap<String, Entry> cache = new ConcurrentHashMap<>();

    private CallerTokenCache() {
    }

    public static void put(String username, String token) {
        cache.put(username, new Entry(token, System.currentTimeMillis()));
    }

    /**
     * Returns the cached token for username, or null if none is cached or the
     * cached entry has aged past TTL_MILLIS (an expired entry is evicted on read).
     */
    public static String get(String username) {
        Entry entry = cache.get(username);
        if (entry == null) {
            return null;
        }
        if (System.currentTimeMillis() - entry.storedAtMillis > TTL_MILLIS) {
            cache.remove(username);
            return null;
        }
        return entry.token;
    }
}
