import { alpha as renamed } from "./a.ts";
export { renamed as outward };
function alpha(value: number) { return value; }
renamed("x");
alpha(1);
