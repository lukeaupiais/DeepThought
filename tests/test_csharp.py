"""C# declarations: types, methods and constructors with namespaced
symbols."""

import importlib.util
import unittest

from deepthought.syntax import extract_csharp, make_parser

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_c_sharp"
)

SOURCE = b"""
using System;

namespace Shop.Cart
{
    public class CartService<T> : Hipstershop.CartService.CartServiceBase
    {
        public CartService(ICartStore store) { }

        [Obsolete]
        public async override Task<Empty> AddItem(AddItemRequest request)
        {
            int Local() { return 1; }
            Func<int> lambda = () => 2;
            return Empty;
        }

        public int Count { get; set; }

        private class Nested
        {
            void Run() { }
        }
    }

    public struct Point { public void Move() { } }
    public interface IStore { Task Add(string id); }
    public record Item(string Id);
    public record Order { public void Pay() { } }
    public enum Kind { A, B }
}

namespace Shop.Other.Inner { class Plain { } }
"""

FILE_SCOPED = b"""
namespace Shop.Scoped;

public class Handler
{
    public static void Handle() { }
}
"""


@unittest.skipUnless(READY, "tree-sitter-c-sharp is not installed")
class CSharpDeclarationTests(unittest.TestCase):
    def extract(self, source):
        tree = make_parser("csharp", ".cs").parse(source)
        return [
            (d.symbol, d.kind, d.line)
            for d in extract_csharp(tree, source, "r", "a.cs")
        ]

    def test_declarations(self):
        self.assertEqual(
            self.extract(SOURCE),
            [
                ("Shop.Cart.CartService", "Class", 6),
                ("Shop.Cart.CartService.CartService", "Constructor", 8),
                # An attribute starts the declaration.
                ("Shop.Cart.CartService.AddItem", "Method", 10),
                ("Shop.Cart.CartService.Nested", "Class", 20),
                ("Shop.Cart.CartService.Nested.Run", "Method", 22),
                ("Shop.Cart.Point", "Struct", 26),
                ("Shop.Cart.Point.Move", "Method", 26),
                ("Shop.Cart.IStore", "Interface", 27),
                ("Shop.Cart.IStore.Add", "Method", 27),
                ("Shop.Cart.Item", "Record", 28),
                ("Shop.Cart.Order", "Record", 29),
                ("Shop.Cart.Order.Pay", "Method", 29),
                ("Shop.Cart.Kind", "Enum", 30),
                ("Shop.Other.Inner.Plain", "Class", 33),
            ],
        )

    def test_file_scoped_namespace(self):
        self.assertEqual(
            self.extract(FILE_SCOPED),
            [
                ("Shop.Scoped.Handler", "Class", 4),
                ("Shop.Scoped.Handler.Handle", "Method", 6),
            ],
        )


if __name__ == "__main__":
    unittest.main()
