# Privacy Policy: SFDC Build Tool (Chrome extension)

DRAFT: not legal advice. Have the owner review, fill the `[...]` fields and publish at a stable public HTTPS URL before submission. Effective date: [date].

Publisher: [legal name / individual], [country]. Contact: [support email].

## What the extension does

SFDC Build Tool turns a plain-language request into Salesforce metadata, lets you review it, and deploys it to the Salesforce org you are logged into in Chrome.

## Information the extension handles, and where it goes

**1. Your Salesforce session (stays in your browser).**
To act as you in your org, the extension reads the Salesforce session cookie (`sid`) for the org in your active tab and uses it to call that org's APIs directly from your browser. The session id and your org's URL are **not sent to our servers** and not sent to any third party other than Salesforce itself.

**2. Your requests and Salesforce metadata (sent to our server and your AI provider).**
When you generate, refine, explain or repair something, the extension sends to our backend `[backend URL]`: the text you type; the metadata you chose to open or build (for example the definition of a Flow, Apex source, object and field definitions); and Salesforce validation or deployment error messages. Our backend sends the relevant parts to the AI provider you selected to produce the result and returns it to you. We do not intentionally include your Salesforce session id, cookies or org URL in these requests. Metadata you open from an org may itself contain org-specific names or identifiers; do not open anything you are not allowed to share with an AI provider.

**3. Your AI provider key (stored locally, sent per request).**
You supply your own API key for Anthropic, Google Gemini or Ollama. It is stored in the extension's local storage on your device. It is sent to our backend with a request so the provider can be called on your behalf, and is not stored by the backend `[confirm]`.

**4. Optional "Share this" `[if enabled in v1]`.**
If you choose to share a result, a snapshot of it is stored with a time limit in a hosted key-value store (Upstash Redis) and protected by a link and password you give to others.

## AI providers

Content from item 2 is processed by the provider you choose, under that provider's terms and privacy policy: Anthropic (anthropic.com), Google (Gemini API), or Ollama (`[local endpoint or api.ollama.com]`). We do not control their retention. Use a key and plan whose data terms fit your organisation's policies.

## Hosting

Our backend runs on `[Heroku]`. The host and its infrastructure providers process standard connection and request logs (IP address, timestamp, URL path). `[Describe retention. Confirm no request bodies are logged.]`

## What we do not do

- We do not sell your data and do not share it for advertising.
- We do not use your data to train models or to build profiles. `[confirm with provider settings]`
- We do not collect browsing history; the extension reads only the address of the active tab, and only to detect whether it is a Salesforce page.
- We do not use remote code; all extension code ships in the package.
- We do not receive your Salesforce session id.

## Limited Use

We use the information described above only to provide and improve the single purpose of this extension (generating, reviewing and deploying Salesforce metadata on your request). We do not transfer it to others except to the AI provider you selected to perform the request, as necessary to comply with law, or as part of a merger or sale with notice to you. We do not use it for advertising, credit or lending decisions, and humans do not read it except with your consent for support, for security, or where required by law.

## Retention and your choices

- Session state used to produce a result is held in the backend's memory and discarded when the session ends or the server restarts `[confirm]`.
- Remove the extension to delete everything it stored locally (settings and keys). You can also clear the key in the panel.
- To ask about or delete data held by us, contact [support email].

## Security

All traffic to our backend and to Salesforce uses HTTPS. The extension requests only the permissions it needs (cookies for Salesforce hosts, local storage, side panel).

## Children

Not directed to children under 16.

## Changes

We will update this page and the effective date when practices change; material changes will be flagged in the extension's release notes.

## Contact

[support email]
