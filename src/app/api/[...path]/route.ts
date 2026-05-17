import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

function getBackendBaseUrl(): string {
  const raw = (process.env.BACKEND_API_BASE_URL ?? "http://127.0.0.1:8000").trim();
  const withoutTrailingSlash = raw.replace(/\/$/, "");
  if (/^https?:\/\//i.test(withoutTrailingSlash)) {
    return withoutTrailingSlash;
  }
  return `http://${withoutTrailingSlash}`;
}

async function proxy(request: NextRequest, pathSegments: string[]) {
  const path = pathSegments.join("/");
  const search = request.nextUrl.search;
  const target = `${getBackendBaseUrl()}/${path}${search}`;

  const headers = new Headers(request.headers);
  headers.delete("host");

  const init: RequestInit = {
    method: request.method,
    headers,
    redirect: "manual",
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  try {
    const upstream = await fetch(target, init);

    if (upstream.status >= 300 && upstream.status < 400) {
      const location = upstream.headers.get("location");
      if (location) {
        return NextResponse.redirect(location, upstream.status);
      }
    }

    const responseHeaders = new Headers(upstream.headers);

    return new NextResponse(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Failed to reach backend service";
    return NextResponse.json(
      {
        detail: `Backend proxy error (${getBackendBaseUrl()}): ${message}`,
      },
      { status: 502 },
    );
  }
}

type RouteContext = { params: Promise<{ path: string[] }> };

async function handle(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  return proxy(request, path);
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const HEAD = handle;
export const OPTIONS = handle;
