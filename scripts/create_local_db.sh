#!/usr/bin/env bash
# Создать базу КТК в локально установленном PostgreSQL (без Docker).
# Запуск:  bash scripts/create_local_db.sh
set -e
DB=ktk_eloyavt
DBUSER=ktk
PASS=ktk

echo "Создаю роль $DBUSER и базу $DB ..."
psql -d postgres <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$DBUSER') THEN
    CREATE ROLE $DBUSER LOGIN PASSWORD '$PASS';
  END IF;
END \$\$;
SQL
psql -d postgres -c "CREATE DATABASE $DB OWNER $DBUSER;" 2>/dev/null || echo "база $DB уже существует — ок"
psql -d postgres -c "GRANT ALL PRIVILEGES ON DATABASE $DB TO $DBUSER;"

echo
echo "Готово. Запуск приложения на локальной БД:"
echo "  export DATABASE_URL=postgresql+asyncpg://$DBUSER:$PASS@localhost:5432/$DB"
echo "  uvicorn backend.main:app --reload"
echo
echo "Таблицы и начальные данные создадутся автоматически при первом старте."
