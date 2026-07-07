#!/bin/bash
set -e

# Validate required env var
if [ -z "$PAD_STAGE_PATH" ]; then
    echo "ERROR: PAD_STAGE_PATH is not set" >&2
    exit 1
fi

if [ ! -f "$PAD_STAGE_PATH" ]; then
    echo "ERROR: PAD not found at $PAD_STAGE_PATH" >&2
    exit 1
fi

# Extract PAD. Under /mendix (not filesystem root) since the container runs as the
# non-root mendixuser, which only has write access under WORKDIR /mendix.
echo "Extracting PAD from $PAD_STAGE_PATH..."
rm -rf /mendix/pad /mendix/pad-extract
unzip -q "$PAD_STAGE_PATH" -d /mendix/pad-extract/

# Handle single top-level directory inside zip
children=(/mendix/pad-extract/*)
if [ ${#children[@]} -eq 1 ] && [ -d "${children[0]}" ]; then
    mv "${children[0]}" /mendix/pad
    rmdir /mendix/pad-extract
else
    mv /mendix/pad-extract /mendix/pad
fi

chmod +x /mendix/pad/bin/start

# The Snowflake JDBC driver bundles Apache Arrow, which reflectively pokes
# java.nio.Buffer.address for off-heap memory management. Java 9+'s module
# system blocks that access by default (InaccessibleObjectException), which
# permanently breaks every JDBC query on this JVM the first time the driver
# builds an Arrow-backed result set (java.lang.ExceptionInInitializerError,
# cached for the JVM's lifetime). The java launcher reads JDK_JAVA_OPTIONS
# automatically since JDK 9, regardless of how bin/start invokes it.
export JDK_JAVA_OPTIONS="--add-opens=java.base/java.nio=ALL-UNNAMED"

# Read file-based secrets from /snowflake/secrets/ and export as env vars
SECRETS_DIR="/secrets"
if [ -d "$SECRETS_DIR" ]; then
    # Fixed mappings
    # directoryPath secrets create a file named secret_string inside the directory
    if [ -f "$SECRETS_DIR/pg_pass/secret_string" ]; then
        RUNTIME_PARAMS_DATABASEPASSWORD="$(cat "$SECRETS_DIR/pg_pass/secret_string")"
        export RUNTIME_PARAMS_DATABASEPASSWORD
    fi
    if [ -f "$SECRETS_DIR/admin_pass/secret_string" ]; then
        M2EE_ADMIN_PASS="$(cat "$SECRETS_DIR/admin_pass/secret_string")"
        RUNTIME_ADMINUSER_PASSWORD="$M2EE_ADMIN_PASS"
        export M2EE_ADMIN_PASS RUNTIME_ADMINUSER_PASSWORD
    fi
    # Mendix license key (RUNTIME_LICENSE_ID arrives as a plain env var, no file needed).
    # Absent entirely when the app runs trial-licensed - the runtime just falls back
    # to trial behavior, no error.
    if [ -f "$SECRETS_DIR/mx_license_key/secret_string" ]; then
        RUNTIME_LICENSE_KEY="$(cat "$SECRETS_DIR/mx_license_key/secret_string")"
        export RUNTIME_LICENSE_KEY
    fi

    # Dynamic constant secrets: mx_const_<module>_<name> -> read variables.conf for env var mapping
    VARS_CONF="/mendix/pad/etc/constants/variables.conf"
    if [ -f "$VARS_CONF" ]; then
        while IFS= read -r line; do
            # Match: "Module.Name" = ${?ENV_VAR_NAME}
            if [[ "$line" =~ ^[[:space:]]*\"([^\"]+)\"[[:space:]]*=[[:space:]]*\$\{\?([^}]+)\} ]]; then
                const_name="${BASH_REMATCH[1]}"
                env_var="${BASH_REMATCH[2]}"
                # Derive secret directory name: MX_CONST_MODULE_NAME (lowercase)
                secret_dir=$(echo "mx_const_${const_name//./_}" | tr '[:upper:]' '[:lower:]')
                secret_path="$SECRETS_DIR/$secret_dir/secret_string"
                if [ -f "$secret_path" ]; then
                    export "$env_var"="$(cat "$secret_path")"
                fi
            fi
        done < "$VARS_CONF"
    fi
fi

# Resolve {SNOWFLAKE_HOST} placeholder in any env vars that contain it.
# Use null-delimited env output so multiline values (e.g. tokens) don't corrupt the parse.
if [ -n "$SNOWFLAKE_HOST" ]; then
    while IFS= read -r -d '' entry; do
        var="${entry%%=*}"
        val="${entry#*=}"
        if [[ "$val" == *"{SNOWFLAKE_HOST}"* ]]; then
            export "$var"="${val//\{SNOWFLAKE_HOST\}/$SNOWFLAKE_HOST}"
        fi
    done < <(env -0)
fi

# Auto-create PG database if it doesn't exist
DBNAME="$RUNTIME_PARAMS_DATABASENAME"
DBHOST=$(echo "$RUNTIME_PARAMS_DATABASEHOST" | cut -d: -f1)
DBPORT=$(echo "$RUNTIME_PARAMS_DATABASEHOST" | cut -d: -f2)
DBPORT="${DBPORT:-5432}"

if [ -n "$DBNAME" ] && [ "$DBNAME" != "postgres" ]; then
    export PGPASSWORD="$RUNTIME_PARAMS_DATABASEPASSWORD"
    export PGSSLMODE="require"
    echo "Checking if database '$DBNAME' exists..."
    # Use || true so a transient connection failure doesn't kill the container via set -e.
    # Use parameterised query ($1) to avoid SQL injection from the database name.
    EXISTS=$(psql -h "$DBHOST" -p "$DBPORT" -U "$RUNTIME_PARAMS_DATABASEUSERNAME" -d postgres \
        -tAc "SELECT 1 FROM pg_database WHERE datname = \$1" -- "$DBNAME" 2>/dev/null) || true
    if [ "$EXISTS" != "1" ]; then
        echo "Creating database '$DBNAME'..."
        psql -h "$DBHOST" -p "$DBPORT" -U "$RUNTIME_PARAMS_DATABASEUSERNAME" -d postgres \
            -c "CREATE DATABASE \"$(printf '%s' "$DBNAME" | sed "s/\"//g")\"" 2>/dev/null || true
        echo "Database '$DBNAME' created (or already existed)."
    else
        echo "Database '$DBNAME' already exists."
    fi
    unset PGPASSWORD PGSSLMODE
fi

exec /mendix/pad/bin/start /mendix/pad/etc/Default
