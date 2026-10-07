#!/bin/bash
# Run once by the postgres image, as the superuser, when it initialises an empty data
# directory. It makes the two roles Corridor connects as and the database they share:
#
#   corridor_owner  owns the schema and runs migrations; nothing else connects as it
#   corridor_app    what the API and the worker connect as; migrations grant it what it
#                   may do, table by table
#
# The image sources this file when it is not executable, which is how it arrives from a
# Windows checkout, so it sets no shell options and leaves none behind.

: "${CORRIDOR_DB_OWNER_PASSWORD:?set CORRIDOR_DB_OWNER_PASSWORD}"
: "${CORRIDOR_DB_APP_PASSWORD:?set CORRIDOR_DB_APP_PASSWORD}"

# The passwords are handed to psql as variables and quoted by it, never spliced into SQL.
psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    --set owner_password="$CORRIDOR_DB_OWNER_PASSWORD" \
    --set app_password="$CORRIDOR_DB_APP_PASSWORD" <<'SQL'
CREATE ROLE corridor_owner LOGIN PASSWORD :'owner_password';
CREATE ROLE corridor_app LOGIN PASSWORD :'app_password';
CREATE DATABASE corridor OWNER corridor_owner;
SQL
