declare module "@example/first" {
    export interface Item { run(): void; }
    export function create(): Item;
}
declare module "@example/second" {
    export interface Item { run(): void; }
    export function create(): Item;
}
