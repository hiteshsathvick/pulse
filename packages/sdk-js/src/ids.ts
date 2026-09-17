/** crypto.randomUUID() is native in every browser this SDK targets and in
 * Node 19+ -- no reason to pull in a uuid package for this. */
export function generateId(): string {
  return crypto.randomUUID();
}
