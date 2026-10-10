/**
 * site-config.ts — single source of truth for site-wide text/contact
 * details used across layout, metadata, and the privacy/terms pages.
 *
 * contactEmail is the real, monitored contact address shown on /privacy,
 * /terms, /data-deletion and the footer (2026-10-10: replaced the former
 * placeholder support@content-automation.app, a domain with no mail
 * records). Keep it a monitored inbox: Meta and TikTok reviewers and users
 * rely on it for data requests.
 */
export const siteConfig = {
  name: "Pickle Batch",
  tagline: "Turn finished videos into a running posting schedule.",
  description:
    "Pickle Batch helps creators batch their finished videos, organize them into a posting schedule, and automate the repetitive work between creating content and publishing it.",
  url: "https://contentautomation.app",
  contactEmail: "malik23stewart23@gmail.com",
} as const;
