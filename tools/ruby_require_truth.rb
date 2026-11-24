# Ground-truth Ruby requires from Prism and Ruby's own feature lookup.
#
# For every .rb file under ROOT, prints one row per `require`,
# `require_relative` and `autoload` whose path is one literal string, in
# the module-reference truth format of tools/ground_truth.py: file, line,
# `import`, target_file, 0, kind.
#
# - require_relative resolves against the requiring file's directory, as
#   Ruby does (`.rb` added unless given). A path to no repository file is
#   not listed.
# - require and autoload search the gem's load path first: each
#   `require_paths` entry of the repository's gemspec (literal values; `lib`
#   when none is set), as `gem` and `bundle exec` put them before every
#   other path. A feature found there is a `module` row. Otherwise Ruby's
#   `$LOAD_PATH.resolve_feature_path` without the repository decides: a
#   standard library or installed gem feature is `external`, and one Ruby
#   cannot find (a gem not installed here) is not listed.
#
# Files under vendor/ and node_modules/ and dot directories are skipped; a
# file Prism reports errors for is listed as unparsed on stderr and not
# indexed. Nothing in the repository is loaded or run.
#
# Usage: ruby tools/ruby_require_truth.rb ROOT > imports.tsv

require "prism"
require "pathname"

root = File.expand_path(ARGV.fetch(0))

def skipped?(relative)
  parts = relative.split("/")
  parts[0...-1].any? { |p| p.start_with?(".") || %w[vendor node_modules].include?(p) }
end

files = Dir.glob("**/*.rb", base: root).reject { |f| skipped?(f) }.sort

# The gemspec's literal require_paths.
require_paths = nil
Dir.glob("*.gemspec", base: root).sort.each do |spec|
  stack = [Prism.parse_file(File.join(root, spec)).value]
  until stack.empty?
    node = stack.pop
    next unless node
    if node.is_a?(Prism::CallNode) && node.name == :require_paths= && node.arguments
      value = node.arguments.arguments.first
      items = value.is_a?(Prism::ArrayNode) ? value.elements : [value]
      require_paths = items.map(&:unescaped) if items.all? { |e| e.is_a?(Prism::StringNode) }
    end
    stack.concat(node.compact_child_nodes)
  end
end
load_path = (require_paths || ["lib"]).map { |p| File.expand_path(p, root) }

def feature(name)
  name.end_with?(".rb") ? name : "#{name}.rb"
end

def in_repository(path, root)
  path.start_with?("#{root}/") && File.file?(path) ? path.delete_prefix("#{root}/") : nil
end

rows = []
indexed = []
files.each do |file|
  result = Prism.parse_file(File.join(root, file))
  unless result.errors.empty?
    warn "unparsed\t#{file}"
    next
  end
  indexed << file
  stack = [result.value]
  until stack.empty?
    node = stack.pop
    next unless node
    stack.concat(node.compact_child_nodes)
    next unless node.is_a?(Prism::CallNode) && node.receiver.nil? && node.arguments
    args = node.arguments.arguments
    path =
      case node.name
      when :require, :require_relative then args.size == 1 ? args[0] : nil
      when :autoload then args.size == 2 ? args[1] : nil
      end
    next unless path.is_a?(Prism::StringNode)
    name = path.unescaped
    line = node.location.start_line
    if node.name == :require_relative
      target = in_repository(File.expand_path(feature(name), File.dirname(File.join(root, file))), root)
      rows << [file, line, target, "module"] if target
      next
    end
    target = load_path.lazy.map { |dir| in_repository(File.expand_path(feature(name), dir), root) }.find(&:itself)
    if target
      rows << [file, line, target, "module"]
    elsif (found = $LOAD_PATH.resolve_feature_path(name)) && !found[1].start_with?("#{root}/")
      rows << [file, line, "-", "external"]
    end
  end
end

puts "# file\tline\tname\ttarget_file\ttarget_line\tkind"
indexed.each { |f| puts "# indexed\t#{f}" }
rows.uniq.sort_by { |r| [r[0], r[1], r[2]] }.each do |file, line, target, kind|
  puts [file, line, "import", target, 0, kind].join("\t")
end
