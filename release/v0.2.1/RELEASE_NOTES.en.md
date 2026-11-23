# SHMAX v0.2.1

- MAX `CONTROL` service events no longer produce unsupported-attachment notices in Telegram. Accompanying text and attachments are forwarded as usual.
- Empty events are skipped before queueing a delivery or creating a Telegram topic.
- Documents in a mixed set of MAX attachments are sent through Telegram's document endpoint.
- Photo and voice URLs can be refreshed through the original MAX message when the required identifiers are present.
- The queue and event journal record safe, specific media failure codes. Temporary failures still retry. A permanently invalid item is skipped so the rest of an album can proceed, with confirmed sends retained in the delivery checkpoint.
- Repeated Telegram transport warnings are grouped by outage. Successful updates are recorded once even if the event journal was temporarily busy.
- Aligned dashboard statuses and actions. The mobile menu supports Escape, and journal and diagnostics content fits narrow screens.
- Storage totals and the local backup list now use the server response.

The upgrade keeps the existing database schema. Previously failed entries remain in delivery history.
