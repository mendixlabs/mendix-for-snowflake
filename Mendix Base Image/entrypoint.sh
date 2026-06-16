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

# Extract PAD
echo "Extracting PAD from $PAD_STAGE_PATH..."
rm -rf /mendix-pad /mendix-pad-extract
unzip -q "$PAD_STAGE_PATH" -d /mendix-pad-extract/

# Handle single top-level directory inside zip
children=(/mendix-pad-extract/*)
if [ ${#children[@]} -eq 1 ] && [ -d "${children[0]}" ]; then
    mv "${children[0]}" /mendix-pad
    rmdir /mendix-pad-extract
else
    mv /mendix-pad-extract /mendix-pad
fi

chmod +x /mendix-pad/bin/start

# Read file-based secrets from /snowflake/secrets/ and export as env vars
SECRETS_DIR="/secrets"
if [ -d "$SECRETS_DIR" ]; then
    # Fixed mappings
    # directoryPath secrets create a file named secret_string inside the directory
    if [ -f "$SECRETS_DIR/pg_pass/secret_string" ]; then
        export RUNTIME_PARAMS_DATABASEPASSWORD="$(cat "$SECRETS_DIR/pg_pass/secret_string")"
    fi
    if [ -f "$SECRETS_DIR/admin_pass/secret_string" ]; then
        export M2EE_ADMIN_PASS="$(cat "$SECRETS_DIR/admin_pass/secret_string")"
        export RUNTIME_ADMINUSER_PASSWORD="$(cat "$SECRETS_DIR/admin_pass/secret_string")"
    fi

    # Dynamic constant secrets: mx_const_<module>_<name> -> read variables.conf for env var mapping
    VARS_CONF="/mendix-pad/etc/constants/variables.conf"
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

# Resolve {SNOWFLAKE_HOST} placeholder in any env vars that contain it
if [ -n "$SNOWFLAKE_HOST" ]; then
    while IFS= read -r vardef; do
        var="${vardef%%=*}"
        val="${vardef#*=}"
        if [[ "$val" == *"{SNOWFLAKE_HOST}"* ]]; then
            export "$var"="${val//\{SNOWFLAKE_HOST\}/$SNOWFLAKE_HOST}"
        fi
    done < <(env)
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

exec /mendix-pad/bin/start /mendix-pad/etc/Default
