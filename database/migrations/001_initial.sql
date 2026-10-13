-- Initial schema for Real-Time Industrial Gas Monitoring and Alert System.
-- Apply once to Supabase SQL Editor. Application startup never runs DDL or clears data.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE zones (
  id SMALLINT PRIMARY KEY CHECK (id BETWEEN 1 AND 4),
  name TEXT NOT NULL UNIQUE,
  gas_name TEXT NOT NULL,
  formula TEXT NOT NULL,
  unit TEXT NOT NULL,
  warning_threshold NUMERIC(12,3) NOT NULL CHECK (warning_threshold > 0),
  critical_threshold NUMERIC(12,3) NOT NULL CHECK (critical_threshold > warning_threshold),
  normal_min NUMERIC(12,3) NOT NULL DEFAULT 0 CHECK (normal_min >= 0),
  normal_max NUMERIC(12,3) NOT NULL CHECK (normal_max > normal_min AND normal_max < warning_threshold),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO zones(id,name,gas_name,formula,unit,warning_threshold,critical_threshold,normal_min,normal_max) VALUES
 (1,'Zone 1','Carbon Monoxide','CO','ppm',35,100,0,12),
 (2,'Zone 2','Methane','CH4','% LEL',10,20,0,4),
 (3,'Zone 3','Hydrogen Sulfide','H2S','ppm',10,20,0,3),
 (4,'Zone 4','Ammonia','NH3','ppm',25,50,0,8)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE readings (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  zone_id SMALLINT NOT NULL REFERENCES zones(id) ON DELETE RESTRICT,
  value NUMERIC(12,3) NOT NULL CHECK (value >= 0),
  unit TEXT NOT NULL,
  measured_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX readings_zone_time_idx ON readings(zone_id, measured_at DESC, id DESC);
CREATE INDEX readings_time_idx ON readings(measured_at DESC);

CREATE TABLE incidents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  zone_id SMALLINT NOT NULL REFERENCES zones(id) ON DELETE RESTRICT,
  gas_name TEXT NOT NULL,
  formula TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('WARNING','CRITICAL')),
  reading NUMERIC(12,3) NOT NULL CHECK (reading >= 0),
  unit TEXT NOT NULL,
  warning_threshold_at_creation NUMERIC(12,3) NOT NULL,
  critical_threshold_at_creation NUMERIC(12,3) NOT NULL,
  threshold NUMERIC(12,3) NOT NULL,
  escalation_threshold NUMERIC(12,3),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  critical_at TIMESTAMPTZ,
  state TEXT NOT NULL DEFAULT 'OPEN' CHECK (state IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
  acknowledged_at TIMESTAMPTZ,
  acknowledged_by TEXT,
  resolved_at TIMESTAMPTZ,
  resolved_by TEXT,
  CHECK (warning_threshold_at_creation > 0),
  CHECK (critical_threshold_at_creation > warning_threshold_at_creation),
  CHECK (threshold > 0),
  CHECK (escalation_threshold IS NULL OR escalation_threshold > 0),
  CHECK (severity <> 'CRITICAL' OR critical_at IS NOT NULL),
  CHECK ((state <> 'ACKNOWLEDGED') OR acknowledged_at IS NOT NULL),
  CHECK ((state <> 'RESOLVED') OR resolved_at IS NOT NULL)
);
CREATE INDEX incidents_zone_state_idx ON incidents(zone_id, state, created_at DESC);
CREATE INDEX incidents_created_idx ON incidents(created_at DESC);
CREATE UNIQUE INDEX incidents_one_active_per_zone_idx ON incidents(zone_id) WHERE state <> 'RESOLVED';

CREATE TABLE notifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id UUID NOT NULL UNIQUE REFERENCES incidents(id) ON DELETE RESTRICT,
  mode TEXT NOT NULL CHECK (mode IN ('mock','real')),
  status TEXT NOT NULL CHECK (status IN ('MOCK_RECORDED','ACCEPTED','PROVIDER_RESPONSE_UNCONFIRMED','FAILED')),
  message TEXT NOT NULL,
  provider_message_id TEXT,
  provider_response JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX notifications_created_idx ON notifications(created_at DESC);

CREATE TABLE audit_events (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_events_created_idx ON audit_events(created_at DESC);
