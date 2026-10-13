-- Keep a separate notification for a warning and a later critical escalation.
-- Existing rows are classified as critical because the original application only
-- created notifications for critical events.
ALTER TABLE notifications
  ADD COLUMN event_severity TEXT NOT NULL DEFAULT 'CRITICAL'
  CHECK (event_severity IN ('WARNING', 'CRITICAL'));

ALTER TABLE notifications
  DROP CONSTRAINT IF EXISTS notifications_incident_id_key;

CREATE UNIQUE INDEX notifications_incident_severity_unique_idx
  ON notifications(incident_id, event_severity);
