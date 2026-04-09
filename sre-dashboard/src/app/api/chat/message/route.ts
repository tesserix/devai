import { NextRequest, NextResponse } from "next/server";

const SRE_API_INTERNAL_URL =
  process.env.DEVAI_SRE_API_INTERNAL_URL || "http://devai-sre.devai.svc.cluster.local:8090";

export async function POST(request: NextRequest) {
  try {
    const body = await request.text();
    const upstream = await fetch(`${SRE_API_INTERNAL_URL}/api/chat/message`, {
      method: "POST",
      headers: {
        "Content-Type": request.headers.get("content-type") || "application/json",
      },
      body,
      cache: "no-store",
    });

    const responseBody = await upstream.text();
    return new NextResponse(responseBody, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") || "application/json",
      },
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "SRE chat proxy request failed.";
    return NextResponse.json({ detail: message }, { status: 502 });
  }
}
