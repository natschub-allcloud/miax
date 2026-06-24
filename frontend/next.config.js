/** @type {import('next').NextConfig} */
const nextConfig = {
  // NOTE: do NOT set `output: 'standalone'` for AWS Amplify Hosting — Amplify's
  // managed Next.js (SSR/WEB_COMPUTE) build packages the server itself, and
  // standalone output conflicts with it. Leave the default output.
};

module.exports = nextConfig;
