-- Synthetic private-v2 history spanning charging, discharging, full/AC,
-- state transitions, gaps, two sleeps, reboot/clock discontinuity, battery
-- replacement, charge/energy health, multiple batteries, and an aggregate-only
-- record.  2024-01-01T00:00:00Z is 1704067200.
INSERT INTO sessions(id, kind, started_at, ended_at, start_percentage,
                     end_percentage, end_reason)
VALUES
    (1, 'charging', 1704067200, 1704067380, 20, 22, 'discharging'),
    (2, 'discharging', 1704067380, 1704088800, 22, 48, 'battery-change'),
    (3, 'charging', 1704088800, NULL, 48, NULL, NULL);

INSERT INTO samples(timestamp, session_id, percentage, state, ac_online,
                    power_w, source, device, power_method, power_approximate,
                    energy_wh, monotonic_s, boottime_s, boot_id, battery_identity)
VALUES
    (1704067200, 1, 20, 'charging',    1, 20, 'fixture', 'BAT0', 'power-now', 0, 10.0, 1000, 1000, 'boot-a', 'battery-a'),
    (1704067260, 1, 21, 'charging',    1, 21, 'fixture', 'BAT0', 'power-now', 0, 10.1, 1060, 1060, 'boot-a', 'battery-a'),
    (1704067320, 1, 22, 'charging',    1, 22, 'fixture', 'BAT0', 'power-now', 0, 10.2, 1120, 1120, 'boot-a', 'battery-a'),
    (1704067380, 2, 22, 'discharging', 0,  8, 'fixture', 'BAT0', 'delta-energy', 1, 10.2, 1180, 1180, 'boot-a', 'battery-a'),
    -- Seven-minute unknown gap.
    (1704067800, 2, 21, 'discharging', 0,  8, 'fixture', 'BAT0', 'delta-energy', 1, 10.1, 1600, 1600, 'boot-a', 'battery-a'),
    (1704067860, 2, 20, 'discharging', 0,  8, 'fixture', 'BAT0', 'delta-energy', 1, 10.0, 1660, 1660, 'boot-a', 'battery-a'),
    (1704067920, 2, 20, 'discharging', 0, NULL, 'fixture', 'BAT0', 'unavailable', 0, 10.0, 1720, 1720, 'boot-a', 'battery-a'),
    -- Resume after the short sleep: monotonic paused, boottime followed wallclock.
    (1704068400, 2, 19, 'discharging', 0, NULL, 'fixture', 'BAT0', 'unavailable', 0,  9.9, 1720, 2200, 'boot-a', 'battery-a'),
    (1704068460, 2, 19, 'discharging', 0,  7, 'fixture', 'BAT0', 'delta-energy', 1,  9.8, 1780, 2260, 'boot-a', 'battery-a'),
    (1704069000, 2, 18, 'discharging', 0,  7, 'fixture', 'BAT0', 'delta-energy', 1,  9.7, 2320, 2800, 'boot-a', 'battery-a'),
    -- Resume after overnight sleep.
    (1704085200, 2, 17, 'discharging', 0, NULL, 'fixture', 'BAT0', 'unavailable', 0,  9.6, 2320, 19000, 'boot-a', 'battery-a'),
    -- Reboot then a deliberate wallclock discontinuity.
    (1704085260, 2, 17, 'discharging', 0, NULL, 'fixture', 'BAT0', 'unavailable', 0,  9.6,   10,   10, 'boot-b', 'battery-a'),
    (1704085620, 2, 16, 'discharging', 0, NULL, 'fixture', 'BAT0', 'unavailable', 0,  9.5,   70,   70, 'boot-b', 'battery-a'),
    (1704085680, 2, 15, 'discharging', 0,  9, 'fixture', 'BAT0', 'delta-energy', 1,  9.4,  130,  130, 'boot-b', 'battery-a'),
    -- Exact UTC-hour boundary and battery replacement/counter reset.
    (1704088800, 3, 48, 'charging',    1, 30, 'fixture', 'BAT1', 'power-now', 0,  3.0, 3250, 3250, 'boot-b', 'battery-b'),
    -- Aggregate-only legacy record: deliberately no battery_samples child row.
    (1704088860, 3, 49, 'charging',    1, 31, 'fixture', 'BAT1', 'power-now', 0,  3.1, 3310, 3310, 'boot-b', 'battery-b'),
    (1704088920, 3, 100, 'full',       1,  0, 'fixture', 'BAT1', 'power-now', 0,  3.2, 3370, 3370, 'boot-b', 'battery-b'),
    (1704088980, 3, 100, 'full',       1,  0, 'fixture', 'BAT1', 'power-now', 0,  3.2, 3430, 3430, 'boot-b', 'battery-b');

INSERT INTO battery_samples(timestamp, device, identity, state, percentage,
                            monotonic_s, boottime_s, boot_id, voltage_now_v,
                            energy_now_wh, energy_full_wh, energy_full_design_wh,
                            charge_now_ah, charge_full_ah, charge_full_design_ah)
VALUES
    (1704067200, 'BAT0', 'battery-a', 'charging', 20, 1000, 1000, 'boot-a', 11.1, NULL, NULL, NULL, 1.0, 3.0, 5.0),
    (1704067260, 'BAT0', 'battery-a', 'charging', 21, 1060, 1060, 'boot-a', 11.1, NULL, NULL, NULL, 1.1, 3.0, 5.0),
    -- Charge-based health change.
    (1704067320, 'BAT0', 'battery-a', 'charging', 22, 1120, 1120, 'boot-a', 11.1, NULL, NULL, NULL, 1.2, 2.9, 5.0),
    (1704067380, 'BAT0', 'battery-a', 'discharging', 22, 1180, 1180, 'boot-a', 11.1, NULL, NULL, NULL, 1.2, 2.9, 5.0),
    -- A second simultaneously observed, energy-based physical battery.
    (1704067380, 'BAT2', 'battery-c', 'discharging', 80, 1180, 1180, 'boot-a', 7.4, 20.0, 30.0, 40.0, NULL, NULL, NULL),
    (1704067800, 'BAT0', 'battery-a', 'discharging', 21, 1600, 1600, 'boot-a', 11.1, NULL, NULL, NULL, 1.1, 2.9, 5.0),
    (1704067860, 'BAT0', 'battery-a', 'discharging', 20, 1660, 1660, 'boot-a', 11.1, NULL, NULL, NULL, 1.0, 2.9, 5.0),
    (1704067920, 'BAT0', 'battery-a', 'discharging', 20, 1720, 1720, 'boot-a', 11.1, NULL, NULL, NULL, 1.0, 2.9, 5.0),
    (1704068400, 'BAT0', 'battery-a', 'discharging', 19, 1720, 2200, 'boot-a', 11.1, NULL, NULL, NULL, 0.9, 2.9, 5.0),
    (1704068460, 'BAT0', 'battery-a', 'discharging', 19, 1780, 2260, 'boot-a', 11.1, NULL, NULL, NULL, 0.9, 2.9, 5.0),
    (1704069000, 'BAT0', 'battery-a', 'discharging', 18, 2320, 2800, 'boot-a', 11.1, NULL, NULL, NULL, 0.8, 2.9, 5.0),
    (1704085200, 'BAT0', 'battery-a', 'discharging', 17, 2320, 19000, 'boot-a', 11.1, NULL, NULL, NULL, 0.7, 2.9, 5.0),
    (1704085260, 'BAT0', 'battery-a', 'discharging', 17, 10, 10, 'boot-b', 11.1, NULL, NULL, NULL, 0.7, 2.9, 5.0),
    (1704085620, 'BAT0', 'battery-a', 'discharging', 16, 70, 70, 'boot-b', 11.1, NULL, NULL, NULL, 0.6, 2.9, 5.0),
    (1704085680, 'BAT0', 'battery-a', 'discharging', 15, 130, 130, 'boot-b', 11.1, NULL, NULL, NULL, 0.5, 2.9, 5.0),
    (1704088800, 'BAT1', 'battery-b', 'charging', 48, 3250, 3250, 'boot-b', 12.0, 3.0, 4.0, 6.0, NULL, NULL, NULL),
    (1704088920, 'BAT1', 'battery-b', 'full', 100, 3370, 3370, 'boot-b', 12.0, 3.2, 4.0, 6.0, NULL, NULL, NULL),
    (1704088980, 'BAT1', 'battery-b', 'full', 100, 3430, 3430, 'boot-b', 12.0, 3.2, 4.0, 6.0, NULL, NULL, NULL);

INSERT INTO sleep_intervals(id, started_at, ended_at, kind, source, boot_id,
                            pre_percentage, post_percentage)
VALUES
    (1, 1704067920, 1704068400, 'sleep', 'clock', 'boot-a', 20, 19),
    (2, 1704069000, 1704085200, 'sleep', 'journal', 'boot-a', 18, 17);

INSERT INTO metadata(key, value) VALUES
    ('fixture', 'pre-v1-comprehensive'),
    ('power_profile', 'not-historically-observed');
