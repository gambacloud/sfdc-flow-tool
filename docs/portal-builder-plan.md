# Portal Builder - plan (Experience Cloud + self-registration + MIAW)

Goal: from one request ("public landing page + registration + secure account area
showing Contact/Account data + chat") produce and deploy the whole stack, as
automated as possible, with a PRE-DEPLOY checklist and a VALIDATE phase.

Status legend: **V** = verified against a real org in this research (2026-10-17),
**D** = from Salesforce docs only, **?** = unknown, must be tested.

## Why this is not just "another artifact type"
Everything the tool builds today is metadata that deploys in one transaction.
A portal is a *pipeline* with ordered phases, some of which are not metadata:

| Phase | Mechanism | Status |
|---|---|---|
| 0 Preflight | SOQL / describe / `listMetadata` against the target org | V |
| 1 Org settings | `Settings` (Communities, ExperienceBundle) via Metadata API | V |
| 2 Create the site shell | **Connect API** `POST /connect/communities` (= `sf community create`), async via `BackgroundOperation` | V |
| 3 Retrieve generated site | Metadata retrieve: Network, CustomSite, DigitalExperienceConfig, DigitalExperienceBundle, guest Profile | V |
| 4 Deploy customisation | Network edits, DigitalExperienceBundle pages (LWR) / ExperienceBundle (Aura), Apex, LWC, Flow, Profile/PermissionSet, SharingSet | partly V |
| 5 Post-deploy data | `NetworkSelfRegistration` record (Account, Apex handler, Execute-As user, shown fields) | V |
| 6 Publish | Connect API publish (`sf community publish`) | ? |
| 7 Validate | smoke tests (below) | ? |

Two hard rules found: a new site cannot be created by deploying
Network+CustomSite+bundle together (circular reference error), and
self-registration settings live in a data object, not in metadata, so a plain
package cannot carry them.

## BOM -> automation level

1. **Licensing / site** - licenses cannot be bought by the tool; preflight only
   *checks* `UserLicense` (Customer Community / Plus / External Identity) and
   that Digital Experiences is enabled. Site shell: phase 2. **Template choice:
   LWR is LWC-only; Aura sites still exist.** Default to LWR ("Build Your Own
   (LWR)") - the self-register component there is `community_login:selfRegister`.
2. **Guest profile / member profile / permission sets** - Profile and PermissionSet
   metadata (guest profile is auto-created with the site and is retrievable). V for
   retrieve, ? for editing object/field permissions safely on a guest profile.
   **Sharing Set** - `SharingSet` metadata type exists in the org (none created
   yet) - ?.
3. **Self-registration** - Network flags + `Configurable` override (V), Apex
   `Auth.ConfigurableSelfRegHandler` class (V, deploys), `NetworkSelfRegistration`
   record (V). Default Account: created by the tool in phase 5. Email verification
   method field exists (`VerificationMethod`) - ? .
4. **Pages** - LWR pages are JSON (`sfdc_cms__view/*/content.json`, `sfdc_cms__route`);
   generating a landing page / secure home from a small component vocabulary
   (rich text, section, button, record detail, related list, custom LWC) is the
   core new generator. "Public vs requires login" is a property of the route - ?.
5. **Components** - Record detail / related list are stock components placed in the
   view JSON; a custom edit form is an LWC, which the tool already generates (V).
6. **MIAW** - `MessagingChannel`, `EmbeddedServiceConfig`, `ServiceChannel`,
   `QueueRoutingConfig`, flows for routing exist in the scratch org's metadata
   types. Docs suggest a *new* deployment is created through Connect API and only
   updated via Metadata API (D). Passing `UserId`/`ContactId` to the widget is
   pre-chat/hidden-prechat configuration - ?. Needs a manually created channel
   first so we can retrieve a real example.

## PRE-DEPLOY checklist (tool runs it, user fixes what fails)
Blocking:
- Target org reachable; API version supported.
- Digital Experiences enabled (`CommunitiesSettings.enableNetworksEnabled`).
- Licenses available: Customer Community / Plus (count free > 0) for the chosen
  user model.
- Site name / url path prefix not already used (`Network`, `CustomSite`).
- Domain exists (`Domain` query) - scratch and DE orgs differ.
- Site admin / guest default owner / Execute-As user exist, active, with Manage Users.
- For MIAW: Omni-Channel and Messaging enabled; `ServiceChannel sfdc_livemessage` present.
- Custom fields/objects the portal pages read exist (or are part of the plan).
Warnings:
- Org is production (never publish without explicit confirmation).
- Person Accounts enabled (changes the registration model).
- External sharing model for Account/Contact is Private (sharing set required).
- Existing guest profile access to Contact/Account (must be none).

## VALIDATE phase (post-deploy)
- Site status, published, URL resolves (HTTP 200 on landing page, no login).
- Guest cannot read Contact/Account (REST as guest, or profile permission scan).
- Self-registration end to end with a throwaway email: user + contact created
  under the default account, correct profile, welcome email sent.
- Logged-in user sees only own Contact and own Account (sharing set check).
- Apex handler tests pass; LWC deployed.
- MIAW: channel active, deployment published, widget present on both pages.
- Cleanup: delete test user/contact.

## Proposed build order (easy -> hard)
1. **Preflight module** + CLI/UI report (pure read-only; most value per effort).
2. **Site shell + self-registration** for LWR: create via Connect API, deploy
   Network edits, handler class, `NetworkSelfRegistration` record, publish, validate.
3. **Pages**: landing + secure home generator (JSON), CTA buttons, record detail
   and related list components.
4. **Sharing set + guest/member profile hardening** with the validation tests.
5. **MIAW** after a manual reference channel is retrieved.
6. **Agentforce agent** (GenAiFunction/GenAiPlugin wrapping the Flows/Apex the tool
   already builds) - last.

## Open questions for the user
- B2C (Person Accounts / one default Account) or B2B (Contact under existing Account)?
- Customer Community vs Plus (affects sharing: sharing set vs sharing rules/roles).
- Need an IdP (Okta) later? Out of scope for v1.
