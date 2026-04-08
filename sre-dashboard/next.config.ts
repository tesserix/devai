import type { NextConfig } from "next";

// In K8s, the SRE dashboard calls the SRE API via internal service DNS.
// Locally, it falls back to localhost:8090.
const SRE_API_INTERNAL_URL =
  process.env.DEVAI_SRE_API_INTERNAL_URL || "http://devai-sre.devai.svc.cluster.local:8090";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${SRE_API_INTERNAL_URL}/api/:path*` },
    ];
  },
};

export default nextConfig;
