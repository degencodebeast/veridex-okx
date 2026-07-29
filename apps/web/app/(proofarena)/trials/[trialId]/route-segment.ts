// Decode the dynamic route subject at the route boundary, exactly once.
//
// Measured through a production `next build` + `next start`: both the server metadata param and
// the hydrated client's `useParams()` value preserve percent escapes. The metadata title and the
// body therefore share this one helper so they cannot drift into naming and fetching different
// trials. HTTP path builders remain the one ENCODING boundary.
//
// Malformed escapes must not make a public route throw. Next 15.5 rejects the malformed wire forms
// before route code runs, but callers outside that transport can still supply one; preserving the
// raw segment keeps the fallback explicit and version-independent.
export function decodeTrialRouteSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}
