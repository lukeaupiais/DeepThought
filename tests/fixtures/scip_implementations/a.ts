export interface Animal { sound(): string; name: string; }
export interface Pet extends Animal { owner: string; }
export class Base { greet(): string { return "hi"; } }
export class Dog extends Base implements Pet {
  name = "d"; owner = "o";
  sound() { return "woof"; }
  greet() { return "wag"; }
}
export class Shadow implements Base { greet() { return "x"; } }
export abstract class Shape { abstract area(): number; }
export class Square extends Shape { area() { return 1; } }
const pet: Pet = { name: "p", owner: "q", sound: () => "" };
export type Alias = Animal;
