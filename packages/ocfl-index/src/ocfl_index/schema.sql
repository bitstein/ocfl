CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE objects (
  object_id         TEXT PRIMARY KEY,
  root_relpath      TEXT NOT NULL UNIQUE,
  spec_version      TEXT NOT NULL,
  digest_algorithm  TEXT NOT NULL,
  head_version      TEXT NOT NULL,
  head_version_num  INTEGER NOT NULL,
  head_created_at   TEXT NOT NULL,
  inventory_digest  TEXT NOT NULL,
  indexed_at        TEXT NOT NULL
);

CREATE TABLE versions (
  object_id    TEXT NOT NULL,
  version_num  INTEGER NOT NULL,
  version      TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  message      TEXT,
  user_name    TEXT,
  user_address TEXT,
  PRIMARY KEY (object_id, version_num),
  FOREIGN KEY (object_id) REFERENCES objects(object_id) ON DELETE CASCADE
);

CREATE TABLE content_paths (
  object_id    TEXT NOT NULL,
  version_num  INTEGER NOT NULL,
  logical_path TEXT NOT NULL,
  digest       TEXT NOT NULL,
  PRIMARY KEY (object_id, version_num, logical_path),
  FOREIGN KEY (object_id, version_num)
    REFERENCES versions(object_id, version_num) ON DELETE CASCADE
);
CREATE INDEX content_paths_by_digest ON content_paths(digest);

CREATE TABLE blobs (
  digest        TEXT PRIMARY KEY,
  size          INTEGER NOT NULL,
  media_type    TEXT,
  first_seen_at TEXT NOT NULL
);

CREATE TABLE ots_attestations (
  proof_digest    TEXT PRIMARY KEY,
  attested_digest TEXT NOT NULL,
  block_height    INTEGER,
  parsed_at       TEXT NOT NULL,
  parse_error     TEXT
);
CREATE INDEX ots_attestations_by_attested ON ots_attestations(attested_digest);

CREATE TABLE ots_inventory (
  object_id    TEXT NOT NULL,
  version_num  INTEGER NOT NULL,
  proof_digest TEXT NOT NULL,
  PRIMARY KEY (object_id, version_num),
  FOREIGN KEY (object_id, version_num)
    REFERENCES versions(object_id, version_num) ON DELETE CASCADE,
  FOREIGN KEY (proof_digest)
    REFERENCES ots_attestations(proof_digest)
);
