# Chrome Web Store listing (draft)

Drafts for the Developer Dashboard "Store listing" tab. Text in `[brackets]` needs an owner decision. Claims below describe the target v1; **do not submit until the org-calls are ported** (see ARCHITECTURE.md section 6), otherwise the listing promises features the build does not have.

## Name

SFDC Build Tool  (45 character limit; the manifest name must match the listing)

## Category / language

Developer Tools. English.

## Short description (max 132 characters)

Describe Salesforce changes in plain language, review them, and deploy to the org you are logged into. No Connected App needed.

(127 characters.)

## Detailed description

SFDC Build Tool turns a plain-language request into Salesforce metadata you can review and deploy: Flows, Apex classes and triggers, Lightning Web Components, Custom Objects and Fields and Permission Sets.

HOW IT WORKS
1. Log in to your Salesforce org in Chrome as you normally do.
2. Open the side panel. The extension shows which org the active tab belongs to.
3. Describe what you want, or open an existing Flow, Apex class, trigger or component from the org to change it.
4. Review the generated result (flow diagram, code, component preview) and approve it.
5. Validate against the org (a check-only deployment), fix any errors with one click, then deploy.

NO CONNECTED APP, NO TOKEN COPYING
The extension uses the Salesforce session you already have in the browser. Nothing has to be approved by your Salesforce admin as a Connected App, and you do not paste tokens anywhere.

YOUR SESSION STAYS IN YOUR BROWSER
Calls to Salesforce (listing, retrieving and deploying metadata) are made by the extension directly from your browser. Your Salesforce session id and your org's URL are never sent to our servers.

BRING YOUR OWN AI KEY
Generation uses the model provider you choose (Anthropic, Google Gemini or Ollama) with your own API key. The key is stored only in your browser (extension local storage) and is sent with your request so the provider can answer it.

WHAT IS SENT TO OUR SERVER
Only what is needed to generate and check metadata: your text request, the metadata you chose to open or build (for example a Flow's definition), Salesforce error messages from a validation, and your AI key for that request. See the privacy policy for the full list.

REQUIREMENTS
- Chrome 116 or later.
- A Salesforce user who can deploy metadata (API Enabled; Modify Metadata or Author Apex, depending on what you deploy).
- An API key for a supported AI provider.

SUPPORT: salesforcedebugtool@gmail.com   PRIVACY POLICY: [URL]

This extension is not affiliated with or endorsed by Salesforce, Inc. Salesforce is a trademark of Salesforce, Inc.

## Notes on accuracy

- v1 scope (owner decision): "Share this", Org Summary, Agent publish and the portal/site builder are not listed. Add one back only after it works in the shipped build (Agent publish also depends on the SFAP spike result, ARCHITECTURE.md section 1).
- The provider list matches `PROVIDERS` in `server.py:170`. Remove Ollama if the hosted backend should not reach an Ollama endpoint.
- "Never sent to our servers" must stay true for the shipped code; `tools/check.py` guards the backend module against touching the cookie, but a review of every request body is still needed once the org client is ported.
