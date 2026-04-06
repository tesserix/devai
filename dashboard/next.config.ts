import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8080/dashboard/api/:path*",
      },
    ];
  },
};

export default nextConfig;
