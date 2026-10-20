# Submission checklist: Unlisted on the Chrome Web Store

Unlisted = installable only via the direct link; not searchable. It still goes through review, and the same policies apply. Items marked **OWNER** can only be done by the account owner (payment, identity, legal, hosting). Items marked **DEV** are engineering.

## A. Account (OWNER)

- [ ] Register a Chrome Web Store developer account at the Developer Dashboard with a Google account intended to own the extension long-term (prefer a team/role account, not a personal one that may be lost).
- [ ] Pay the one-time developer registration fee (US$5 at time of writing; check the dashboard).
- [ ] Turn on 2-Step Verification on that Google account (required to publish).
- [ ] Contact email in the dashboard, and **verify** it (verification email).
- [ ] Declare trader / non-trader status under the EU Digital Services Act. If trader (you offer this commercially), provide and verify name, address, phone and email; these are shown publicly on the listing for EU users. If non-trader, say so.
- [ ] (Optional) Publisher verification / a verified website domain if you want it shown on the listing.

## B. Hosting and legal (OWNER)

- [ ] Publish `PRIVACY_POLICY.md` (after filling every `[...]`) at a stable public HTTPS URL; paste into the "Privacy policy" field.
- [ ] Support email or support page URL; monitored.
- [ ] Confirm the facts the policy asserts (open questions in `PERMISSIONS_AND_PRIVACY_FORMS.md`): retention on Heroku, logs, Upstash, provider terms.
- [ ] Decide trademark wording: the "not affiliated with Salesforce" line stays.
- [ ] Production backend: HTTPS origin chosen, CORS allows `chrome-extension://<extension id>`; rate limiting in place since the backend is reachable from any install.

## C. Build (DEV)

- [ ] Port org calls (list, retrieve, validate, deploy, setup URL, agent publish, portal) per ARCHITECTURE.md; remove the stubs.
- [ ] Backend split done: no org credentials in any request model.
- [ ] Set the real `BACKEND_URL` in `extension/lib/config.js`, `host_permissions` and CSP `connect-src` in `manifest.json`; `python extension/tools/check.py` passes.
- [ ] Final icons (16/32/48/128) replace placeholders; `python extension/tools/sync_ui.py` re-run after any UI change.
- [ ] Resolve `metadata-kb-worker.js` (missing) or hide Org Summary.
- [ ] Decide mermaid handling (ARCHITECTURE.md section 5) and note the `eval` string for the reviewer if kept.
- [ ] Manual test on a clean Chrome profile: load unpacked, log in to a scratch org, open panel, validate and deploy a Flow, Apex class and LWC; sandbox, My Domain and Experience Cloud hosts; two orgs open at once.
- [ ] Bump `version` in `manifest.json`; zip the **contents** of `extension/` (manifest at the zip root), excluding `tools/`. Keep the zip under the store limit; the vendored mermaid is about 3.5 MB.

## D. Listing (OWNER with DEV help)

- [ ] Paste name, short and long description from `LISTING.md`; category Developer Tools; language English.
- [ ] Upload store icon 128x128, small promo tile 440x280, 1 to 5 screenshots 1280x800 (see `ASSETS.md`).
- [ ] Visibility: choose **Unlisted** (Distribution tab). Regions: all, or a chosen list.
- [ ] Privacy practices tab: single purpose, permission justifications, remote-code = No, data-usage boxes and the three certifications, from `PERMISSIONS_AND_PRIVACY_FORMS.md`.
- [ ] Add reviewer test instructions: reviewers cannot use your org. Provide a test path (a scratch/developer org login, or a short video) and note that a Salesforce login and an AI key are needed. Missing test access is a common rejection reason.

## E. Submit and follow up

- [ ] Submit for review (optionally choose "defer publishing" to control the release moment).
- [ ] Expect a review of about a few days, longer for broad host permissions or `cookies`; be ready to answer questions about why `cookies` and Salesforce-wide host access are needed (text is prepared).
- [ ] After approval, copy the unlisted store URL and share it with users. Note the extension id for the backend CORS allow-list.
- [ ] Keep privacy policy, listing and dashboard answers in sync with every release that changes data handling or permissions; adding permissions later disables the extension for existing users until they accept.
