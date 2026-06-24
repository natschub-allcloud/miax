import { NextResponse } from "next/server";

const API_URL = process.env.API_URL ?? "";
const API_KEY = process.env.API_KEY ?? "";

export async function GET() {
  try {
    const res = await fetch(`${API_URL}stats`, {
      method: "GET",
      headers: {
        "x-api-key": API_KEY,
      },
      // Always fetch fresh counts.
      cache: "no-store",
    });

    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: `Proxy error: ${error instanceof Error ? error.message : "Unknown"}` },
      { status: 500 }
    );
  }
}
