# Ground-truth Ruby declarations from Ruby's own parser (Ripper).
#
# For every .rb file under ROOT (or the files given), prints one
# tab-separated row per module, class and method in the declaration-truth
# format of tools/c_declaration_truth.py: file, line, name, kind, begin_line.
# Kinds: `module`, `type` (a class), `method` (a def in a class, module or
# singleton class, and def self.x) and `function` (a top-level def). A file
# Ripper cannot parse is listed as unparsed on stderr and not indexed.
#
# Usage: ruby tools/ruby_declaration_truth.rb ROOT [FILE ...] > truth.tsv

require "ripper"
require "find"

def name_of(node)
  case node&.first
  when :@const, :@ident, :@op, :@kw, :@backtick then [node[1], node[2][0]]
  when :const_ref, :top_const_ref, :var_ref then name_of(node[1])
  when :const_path_ref then name_of(node[2])
  end
end

def walk(node, file, scope, rows)
  return unless node.is_a?(Array)
  case node.first
  when :module, :class
    name, line = name_of(node[1])
    rows << [file, line, name, node.first == :module ? "module" : "type", line] if name
    walk(node.last, file, :type, rows)
    return
  when :sclass
    walk(node[2], file, :type, rows)
    return
  when :def
    name, line = name_of(node[1])
    rows << [file, line, name, scope == :type ? "method" : "function", line] if name
    walk(node.last, file, :body, rows)
    return
  when :defs
    name, line = name_of(node[3])
    rows << [file, line, name, "method", line] if name
    walk(node.last, file, :body, rows)
    return
  end
  node.each { |child| walk(child, file, scope, rows) }
end

root = File.expand_path(ARGV.shift)
files = ARGV.empty? ? [] : ARGV.map { |f| File.join(root, f) }
if files.empty?
  Find.find(root) do |path|
    Find.prune if File.basename(path) == ".git"
    files << path if path.end_with?(".rb") && File.file?(path)
  end
end
rows, indexed = [], []
files.sort.each do |path|
  relative = path.delete_prefix(root + "/")
  tree = Ripper.sexp(File.read(path, encoding: "UTF-8"))
  if tree.nil?
    warn "#{relative}: unparsed"
    next
  end
  indexed << relative
  walk(tree, relative, :top, rows)
end
puts "# file\tline\tname\tkind\tbegin_line"
indexed.sort.each { |f| puts "# indexed\t#{f}" }
rows.uniq.sort.each { |row| puts row.join("\t") }
