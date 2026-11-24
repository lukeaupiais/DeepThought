# Ruby declarations as Prism (Ruby's own parser) reads them, one JSON
# object per line: file, line and column of the name, name, kind, symbol.
# The scoping rules are those of deepthought.syntax.extract_ruby, applied
# to Prism's tree; see tools/compare_ruby_declarations_with_prism.py.
#
# Usage: ruby tools/ruby_declarations_prism.rb ROOT FILE...
require "json"
require "prism"

root = ARGV.shift

def constant_path(node)
  names = []
  while node.is_a?(Prism::ConstantPathNode)
    child = node.respond_to?(:child) ? node.child : node
    names.unshift(child.respond_to?(:name) ? child.name.to_s : child.slice)
    return [names, true, child] if node.parent.nil?
    node = node.parent
  end
  return nil unless node.is_a?(Prism::ConstantReadNode)
  names.unshift(node.name.to_s)
  [names, false, nil]
end

def name_location(node)
  path = node.constant_path
  path.is_a?(Prism::ConstantPathNode) ? path.child.location : path.location
end

def visit(node, scope, in_type, file, out)
  case node
  when Prism::ClassNode, Prism::ModuleNode
    path = constant_path(node.constant_path)
    return if path.nil?
    names, absolute, = path
    inner = (absolute ? [] : scope) + names
    location = name_location(node)
    out << {
      file: file, line: location.start_line, column: location.start_column,
      name: names.last, kind: node.is_a?(Prism::ClassNode) ? "Class" : "Module",
      symbol: inner.join(".")
    }
    node.compact_child_nodes.each { |c| visit(c, inner, true, file, out) }
  when Prism::DefNode
    location = node.name_loc
    out << {
      file: file, line: location.start_line, column: location.start_column,
      name: node.name.to_s, kind: in_type || node.receiver ? "Method" : "Function",
      symbol: (scope + [node.name.to_s]).join(".")
    }
  else
    node.compact_child_nodes.each { |c| visit(c, scope, in_type, file, out) }
  end
end

ARGV.each do |file|
  result = Prism.parse_file(File.join(root, file))
  out = []
  visit(result.value, [], false, file, out)
  errors = result.errors.map { |e| e.location.start_line }
  puts JSON.generate({ file: file, declarations: out, error_lines: errors })
end
