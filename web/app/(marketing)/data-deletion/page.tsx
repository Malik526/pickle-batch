import type { Metadata } from "next";
import Link from "next/link";
import { Container } from "@/components/ui/Container";
import { siteConfig } from "@/lib/site-config";

/**
 * /data-deletion — public User Data Deletion instructions, the page Meta's
 * App Dashboard links to under App settings > Basic > User data deletion
 * ("Data deletion instructions URL"). Static and public like /privacy and
 * /terms: no login, no client-side JavaScript needed to read it.
 *
 * Only describes what Pickle Batch actually does today: deletion requests
 * are handled manually by email, and the two in-app controls that exist
 * (delete a video in Library, disconnect a platform in Settings). It makes
 * no promise of deletion timing, automation, backups, or deletion from
 * Meta's own systems. This is not Meta's Data Deletion Callback (a backend
 * endpoint), which doesn't exist yet.
 */

export const metadata: Metadata = {
  title: "User Data Deletion",
  description: `How to request deletion of your ${siteConfig.name} data.`,
};

const LAST_UPDATED = "October 10, 2026";

export default function DataDeletionPage() {
  const mailto = `mailto:${siteConfig.contactEmail}?subject=${encodeURIComponent("Data Deletion Request")}`;

  return (
    <section className="py-16 sm:py-24">
      <Container>
        <div className="mx-auto max-w-2xl legal-content">
          <p className="text-sm font-medium text-accent">Legal</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight text-ink">User Data Deletion</h1>
          <p className="mt-2 text-sm text-ink-muted">Last updated: {LAST_UPDATED}</p>

          <p className="mt-8">
            {siteConfig.name} allows users to request deletion of personal data associated with their{" "}
            {siteConfig.name} account and connected social-platform integrations, including Instagram and
            other Meta integrations.
          </p>

          <h2>How to request deletion</h2>
          <ol>
            <li>
              Email us at <a href={mailto}>{siteConfig.contactEmail}</a>.
            </li>
            <li>Use the subject &ldquo;Data Deletion Request&rdquo;.</li>
            <li>
              Send the request from the email address associated with your {siteConfig.name} account
              when possible.
            </li>
            <li>
              We may ask for reasonable verification before deleting data, to prevent unauthorized
              deletion requests.
            </li>
          </ol>
          <p>
            Deletion requests are currently handled manually. We will confirm by email once your request
            has been completed.
          </p>
          <p>
            You can also remove some data yourself while signed in: deleting a video in your Library
            removes that video and its stored file, and disconnecting a platform in Settings removes the
            access tokens {siteConfig.name} stored for that connection. A video that is scheduled or has
            already been posted can&rsquo;t be deleted from the Library; include it in an email request
            instead.
          </p>

          <h2>What we may delete</h2>
          <p>Data {siteConfig.name} stores about you and your content, where it applies to your account:</p>
          <ul>
            <li>your account information, such as your name and email address;</li>
            <li>identifiers of the social-platform accounts you connected;</li>
            <li>the encrypted access tokens for those connections, and connection status records;</li>
            <li>videos you uploaded, their stored files, and their metadata (such as file details);</li>
            <li>captions, hashtags, and transcripts stored with your videos;</li>
            <li>your posting schedule and the scheduled content in your queue;</li>
            <li>records of posts {siteConfig.name} published or attempted to publish for you;</li>
            <li>technical records of your uploads, such as upload timing and errors.</li>
          </ul>

          <h2>Connected Instagram and Meta accounts</h2>
          <p>
            A deletion request to {siteConfig.name} covers data that {siteConfig.name} stores. It does not
            delete your Instagram, Facebook, Threads, or other Meta account, and it does not delete
            information held by Meta, including posts already published to Instagram. To delete your Meta
            account or content stored by Meta, use the tools Meta provides.
          </p>

          <h2>Revoking {siteConfig.name}&rsquo;s access</h2>
          <p>
            You can revoke {siteConfig.name}&rsquo;s access to your Instagram or other Meta account through
            the apps and integrations settings Meta provides, or disconnect the account in{" "}
            {siteConfig.name}&rsquo;s Settings. Revoking access prevents {siteConfig.name} from accessing new
            data from that connection. It does not by itself delete data {siteConfig.name} already stored;
            to have that deleted, send a deletion request as described above.
          </p>

          <h2>Data we may be required to retain</h2>
          <p>
            Some information may be retained when required for security, fraud prevention, legal
            compliance, dispute resolution, or other legitimate obligations. Where applicable, retained
            information will no longer be used for ordinary product purposes.
          </p>

          <h2>Privacy Policy</h2>
          <p>
            For more information about how {siteConfig.name} handles personal information, see our{" "}
            <Link href="/privacy">Privacy Policy</Link>.
          </p>

          <h2>Contact</h2>
          <p>
            Questions about this page or your data can be sent to{" "}
            <a href={`mailto:${siteConfig.contactEmail}`}>{siteConfig.contactEmail}</a>.
          </p>
        </div>
      </Container>
    </section>
  );
}
