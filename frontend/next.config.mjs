/** @type {import('next').NextConfig} */
const nextConfig = {
  // Phase 31: traces and bundles only the production deps the built server
  // actually imports into .next/standalone -- the Docker runtime stage ships
  // that plus .next/static, no npm CLI, no devDependencies, no full
  // node_modules. See frontend/Dockerfile.
  output: "standalone",
};

export default nextConfig;
