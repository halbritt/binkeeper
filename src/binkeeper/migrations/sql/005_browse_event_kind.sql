-- Add the `browse` retrieval-outcome kind (bin_trip_event.v3, BINK-20).
--
-- A browse is an opened-took-nothing look: the owner physically opened this
-- bin at a site (which re-confirms location like fetch/confirm) but took
-- nothing — evidence the passport is lying about the contents. Constraints
-- carry the stable names installed by migration 004.
ALTER TABLE bin_trip_events
    DROP CONSTRAINT bin_trip_events_event_kind_check,
    DROP CONSTRAINT bin_trip_events_trip_id_presence_check,
    DROP CONSTRAINT bin_trip_events_site_presence_check;

ALTER TABLE bin_trip_events
    ADD CONSTRAINT bin_trip_events_event_kind_check CHECK (
        event_kind IN (
            'place','open','load','arrive','close','confirm','contradict',
            'fetch','not_found','browse')),
    ADD CONSTRAINT bin_trip_events_trip_id_presence_check CHECK (
        event_kind IN ('place','confirm','contradict','fetch','not_found','browse')
        OR trip_id IS NOT NULL),
    ADD CONSTRAINT bin_trip_events_site_presence_check CHECK (
        event_kind NOT IN ('place','arrive','confirm','fetch','not_found','browse')
        OR site IS NOT NULL);
