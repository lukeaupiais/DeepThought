// The protobuf-es 1 (protoc-gen-es v1) form of order.proto, cut down to its
// type registrations.
import { Message, proto3 } from "@bufbuild/protobuf";

export enum Channel {
  CHANNEL_UNSPECIFIED = 0,
}
proto3.util.setEnumType(Channel, "shop.v1.Channel", [
  { no: 0, name: "CHANNEL_UNSPECIFIED" },
]);

export class Item extends Message<Item> {
  static readonly runtime: typeof proto3 = proto3;
  static readonly typeName = "shop.v1.Item";
}

export class Item_Tag extends Message<Item_Tag> {
  name = "";
  static readonly runtime: typeof proto3 = proto3;
  static readonly typeName = "shop.v1.Item.Tag";
}
