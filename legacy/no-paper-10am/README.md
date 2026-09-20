# Archived: Local 10:00 NO Paper Strategy

This directory preserves the retired fixed-time, NO-only paper strategy for historical research.

It is deliberately isolated from the active Hermes weather trading system:

- The active system does not import this code.
- Its LaunchAgent is disabled and stored here with a `.disabled` suffix.
- The installer and test entry points also have `.disabled` suffixes.
- The archived script resolves relative paths inside this directory, so it cannot silently reconnect to the active database using its old configuration.
- Historical `weather_no_paper_*` tables remain in the main SQLite database as read-only evidence. The active system neither creates nor queries them.

Do not reactivate this module as part of the production pipeline. Any future comparison should use an explicit offline replay against a database copy.
