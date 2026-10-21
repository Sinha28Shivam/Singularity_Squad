/** Shared assertions for the sourcing/payments page tests. */
// Status-like wording that must never appear ("nothing is awarded" in the required banner is allowed).
export const FORBIDDEN = /\bPaid\b|\bAwarded\b|\bPO sent\b|\bpayment (was )?made\b/;
