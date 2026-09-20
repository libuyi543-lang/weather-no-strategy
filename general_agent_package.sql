ATTACH DATABASE 'data/weather_market_monitor.sqlite3' AS src;
PRAGMA foreign_keys=OFF;

CREATE TABLE stations AS SELECT * FROM src.stations WHERE city IN ('Beijing','Chengdu','Chongqing','Guangzhou','Qingdao','Shanghai','Wuhan');
CREATE TABLE events AS SELECT * FROM src.events WHERE city IN ('Beijing','Chengdu','Chongqing','Guangzhou','Qingdao','Shanghai','Wuhan');
CREATE TABLE markets AS SELECT m.* FROM src.markets m JOIN events e ON e.event_id=m.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE market_resolutions AS SELECT r.* FROM src.market_resolutions r JOIN markets m ON m.market_id=r.market_id;
CREATE TABLE market_snapshots AS SELECT s.* FROM src.market_snapshots s JOIN events e ON e.event_id=s.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';

CREATE TABLE weather_observations AS
  SELECT o.* FROM src.weather_observations o JOIN stations s ON s.station_id=o.station_id WHERE o.sample_local_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE fast_metar_reports AS
  SELECT r.* FROM src.fast_metar_reports r JOIN stations s ON s.station_id=r.station_id WHERE r.observation_time_utc >= '2026-08-15T00:00:00+00:00' AND r.observation_time_utc < '2026-08-25T00:00:00+00:00';
CREATE TABLE external_forecasts AS
  SELECT f.* FROM src.external_forecasts f JOIN stations s ON s.station_id=f.station_id WHERE f.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE ensemble_forecasts AS
  SELECT f.* FROM src.ensemble_forecasts f JOIN stations s ON s.station_id=f.station_id WHERE f.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE windy_forecasts AS
  SELECT f.* FROM src.windy_forecasts f JOIN stations s ON s.station_id=f.station_id WHERE f.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE forecast_model_runs AS
  SELECT f.* FROM src.forecast_model_runs f JOIN stations s ON s.station_id=f.station_id WHERE f.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE remote_sensing_snapshots AS
  SELECT r.* FROM src.remote_sensing_snapshots r JOIN stations s ON s.station_id=r.station_id WHERE r.slot_utc >= '2026-08-15T00:00:00+00:00' AND r.slot_utc < '2026-08-25T00:00:00+00:00';
CREATE TABLE weather_process_states AS
  SELECT p.* FROM src.weather_process_states p JOIN stations s ON s.station_id=p.station_id WHERE p.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE weather_ridge_v2_snapshots AS
  SELECT r.* FROM src.weather_ridge_v2_snapshots r JOIN events e ON e.event_id=r.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE weather_forecast_evaluations AS
  SELECT f.* FROM src.weather_forecast_evaluations f JOIN stations s ON s.station_id=f.station_id WHERE f.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE weather_resolution_labels AS
  SELECT l.* FROM src.weather_resolution_labels l JOIN events e ON e.event_id=l.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';

CREATE TABLE weather_dual_reviews AS SELECT r.* FROM src.weather_dual_reviews r JOIN events e ON e.event_id=r.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE weather_dual_actions AS SELECT a.* FROM src.weather_dual_actions a JOIN events e ON e.event_id=a.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE weather_dual_fills AS SELECT f.* FROM src.weather_dual_fills f JOIN events e ON e.event_id=f.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE weather_dual_candidate_audits AS SELECT a.* FROM src.weather_dual_candidate_audits a JOIN events e ON e.event_id=a.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE weather_dual_outcome_reviews AS SELECT r.* FROM src.weather_dual_outcome_reviews r JOIN events e ON e.event_id=r.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE weather_dual_positions AS SELECT p.* FROM src.weather_dual_positions p JOIN events e ON e.event_id=p.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';

CREATE TABLE weather_ai_agent_cycles AS SELECT c.* FROM src.weather_ai_agent_cycles c JOIN events e ON e.event_id=c.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE weather_ai_agent_actions AS SELECT a.* FROM src.weather_ai_agent_actions a JOIN src.weather_ai_agent_cycles c ON c.cycle_id=a.cycle_id JOIN events e ON e.event_id=c.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE weather_ai_agent_fills AS SELECT f.* FROM src.weather_ai_agent_fills f JOIN events e ON e.event_id=f.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';
CREATE TABLE weather_ai_opportunity_evaluations AS SELECT o.* FROM src.weather_ai_opportunity_evaluations o JOIN events e ON e.event_id=o.event_id WHERE e.target_date BETWEEN '2026-08-15' AND '2026-08-24';

CREATE INDEX idx_events_city_date ON events(city,target_date);
CREATE INDEX idx_markets_event ON markets(event_id);
CREATE INDEX idx_market_snapshots_event_slot ON market_snapshots(event_id,slot_utc);
CREATE INDEX idx_weather_observations_station_date ON weather_observations(station_id,sample_local_date,slot_utc);
CREATE INDEX idx_fast_metar_station_time ON fast_metar_reports(station_id,observation_time_utc);
CREATE INDEX idx_external_station_date ON external_forecasts(station_id,target_date,slot_utc);
CREATE INDEX idx_ensemble_station_date ON ensemble_forecasts(station_id,target_date,slot_utc);
CREATE INDEX idx_windy_station_date ON windy_forecasts(station_id,target_date,slot_utc);
CREATE INDEX idx_remote_station_time ON remote_sensing_snapshots(station_id,slot_utc);
CREATE INDEX idx_process_station_time ON weather_process_states(station_id,target_date,slot_utc);
CREATE INDEX idx_ridge_event_time ON weather_ridge_v2_snapshots(event_id,feature_as_of_utc);

PRAGMA user_version=1;
DETACH DATABASE src;
