export function shouldSuppressDataEvent(payload) {
  return payload?.manifest?.status === "validation_failed";
}
