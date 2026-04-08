import type { NextConfig } from "next";

// In K8s, the dashboard calls the API via internal service DNS.
// Locally, it falls back to localhost:8080.
const API_INTERNAL_URL =
  process.env.DEVAI_API_INTERNAL_URL || "http://devai-api.devai.svc.cluster.local:8080";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_INTERNAL_URL}/dashboard/api/:path*`,
      },
      {
        source: "/chat/:path*",
        destination: `${API_INTERNAL_URL}/chat/:path*`,
      },
    ];
  },
};

export default nextConfig;
