import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Backend traffic is proxied at runtime via src/app/api/[...path]/route.ts
  // so BACKEND_API_BASE_URL from Render does not need to be present at build time.
};

export default nextConfig;
