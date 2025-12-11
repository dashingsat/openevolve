#!/bin/bash
set -e

# Configuration
DB_NAME="job_db"
PG_USER=${PG_USER:-"postgres"}
PG_HOST=${PG_HOST:-"localhost"}
PG_PORT=${PG_PORT:-"5432"}
DATA_URL="http://event.cwi.nl/da/job/imdb.tgz"
DATA_DIR="imdb_data"

if ! command -v psql &> /dev/null; then
    echo "Error: psql is not installed."
    exit 1
fi

if ! command -v wget &> /dev/null; then
    echo "Error: wget is not installed."
    exit 1
fi

echo "=== 1. Setting up Database '$DB_NAME' ==="
# Check if DB exists
if psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
    echo "Database '$DB_NAME' already exists. Skipping creation."
else
    createdb -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$DB_NAME"
    echo "Database created."
fi

echo "=== 2. Downloading Data ==="
if [ ! -d "$DATA_DIR" ]; then
    mkdir -p "$DATA_DIR"
fi

if [ ! -f "$DATA_DIR/imdb.tgz" ]; then
    echo "Downloading imdb.tgz (approx 3.6GB)..."
    wget -c "$DATA_URL" -O "$DATA_DIR/imdb.tgz"
else
    echo "imdb.tgz already present."
fi

echo "=== 3. Extracting Data ==="
# Check if a sample CSV exists to avoid re-extracting
if [ ! -f "$DATA_DIR/title.csv" ]; then
    echo "Extracting CSV files..."
    tar -xzvf "$DATA_DIR/imdb.tgz" -C "$DATA_DIR"
else
    echo "CSV files already extracted."
fi

echo "=== 4. Loading Schema ==="
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$DB_NAME" -f job_data/schema.sql

echo "=== 5. Loading Data (this may take a while) ==="
# List of tables in correct order (dependency wise, though CSV load order matters less if constraints aren't active yet)
# We use the list from the README
TABLES=(
    "aka_name" "aka_title" "cast_info" "char_name" "comp_cast_type"
    "company_name" "company_type" "complete_cast" "info_type" "keyword"
    "kind_type" "link_type" "movie_companies" "movie_info" "movie_info_idx"
    "movie_keyword" "movie_link" "name" "person_info" "role_type" "title"
)

for table in "${TABLES[@]}"; do
    csv_file="$DATA_DIR/$table.csv"
    if [ -f "$csv_file" ]; then
        echo "Loading $table..."
        psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$DB_NAME" -c "\copy $table FROM '$csv_file' WITH (FORMAT csv, ESCAPE '\');"
    else
        echo "Warning: $csv_file not found."
    fi
done

echo "=== 6. Installing Extensions ==="
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS hypopg;"

echo "=== Done! ==="
echo "Connection string: postgresql://$PG_USER@$PG_HOST:$PG_PORT/$DB_NAME"
