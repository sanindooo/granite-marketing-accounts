import Database from "better-sqlite3";
import { resolve } from "path";

const DB_PATH =
  process.env.GRANITE_DB ||
  resolve(process.cwd(), "..", ".state", "pipeline.db");

const globalForDb = globalThis as unknown as {
  db: Database.Database | undefined;
};

function createDatabase(): Database.Database {
  const db = new Database(DB_PATH, { readonly: false });

  db.pragma("journal_mode = WAL");
  db.pragma("synchronous = NORMAL");
  db.pragma("foreign_keys = ON");
  db.pragma("busy_timeout = 30000");
  db.pragma("cache_size = -64000");
  db.pragma("mmap_size = 268435456");
  db.pragma("temp_store = MEMORY");

  return db;
}

export const db = globalForDb.db ?? createDatabase();
if (process.env.NODE_ENV !== "production") globalForDb.db = db;
