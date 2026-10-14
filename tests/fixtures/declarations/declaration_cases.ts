export interface Service {
  execute(value: string): void;
}

export function createService() {
  const normalize = (value: string) => value.trim();
  return normalize;
}
