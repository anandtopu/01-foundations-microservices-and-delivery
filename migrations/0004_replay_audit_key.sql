-- PR #5 review: the replay audit must name the ops KEY, not only its client. Every ops key shares
-- client_id 'meridian-ops', so without this two operators' replays look identical (section 9,
-- Repudiation). The fingerprint is a prefix of the key's SHA-256, never the key.
ALTER TABLE dead_letter_replays ADD COLUMN ops_key_id text NOT NULL DEFAULT '';
