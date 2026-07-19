-- Add the retrieval-outcome event kinds (bin_trip_event.v2, BINK-19).
--
-- `fetch` records a successful retrieval (bin in hand at a site; re-confirms
-- location like `confirm`). `not_found` records a one-tap miss (the owner
-- looked where the fold claims and the bin was absent; shocks confidence like
-- `contradict`). Both carry a bin_code and a site and ride outside any trip.
--
-- The three event-kind CHECK constraints from 001 were created unnamed, so
-- they are dropped by definition match and re-added under stable names.
DO $$
DECLARE
    con RECORD;
BEGIN
    FOR con IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'bin_trip_events'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%event_kind%'
    LOOP
        EXECUTE format('ALTER TABLE bin_trip_events DROP CONSTRAINT %I', con.conname);
    END LOOP;
END $$;

ALTER TABLE bin_trip_events
    ADD CONSTRAINT bin_trip_events_event_kind_check CHECK (
        event_kind IN (
            'place','open','load','arrive','close','confirm','contradict','fetch','not_found')),
    ADD CONSTRAINT bin_trip_events_bin_code_presence_check CHECK (
        (event_kind IN ('open','close')) = (bin_code IS NULL)),
    ADD CONSTRAINT bin_trip_events_trip_id_presence_check CHECK (
        event_kind IN ('place','confirm','contradict','fetch','not_found')
        OR trip_id IS NOT NULL),
    ADD CONSTRAINT bin_trip_events_site_presence_check CHECK (
        event_kind NOT IN ('place','arrive','confirm','fetch','not_found')
        OR site IS NOT NULL);
