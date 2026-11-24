# Table names of a Rails app's models, computed by ActiveRecord itself.
#
#   ruby tools/rails_table_oracle.rb CHECKOUT > tables.json
#
# Needs Ruby 3.3+ (Prism) and the activerecord gem; it never loads the app.
# Model classes are found with Prism: class nesting, superclass, a literal
# `self.table_name =`, `self.abstract_class = true`, `primary_abstract_class`,
# and a literal `self.table_name_prefix` method or assignment. The app's
# inflections are read the same way (literal acronym, irregular,
# uncountable, plural and singular calls) and never run. The classes are
# rebuilt with the same names and hierarchy on top of ActiveRecord::Base and
# ActiveRecord is asked each one's `table_name`, so naming (inflection, STI
# base class, nested-model and module prefixes) is ActiveRecord's own code.
#
# Output: `models` (class, files that open it, table) and `not_counted`,
# models whose table this cannot know or that only tests declare:
# - table_name_not_literal: `self.table_name =` with a computed value, or a
#   `def self.table_name`;
# - prefix_not_literal: the same for `table_name_prefix`;
# - test_only: every file that opens the class is under spec/ or test/.
# Files under vendor/ and node_modules/ are not read.
require "prism"
require "json"
require "active_record"
AR_BASE = ActiveRecord::Base

root = ARGV.fetch(0)
inflections = File.join(root, "config/initializers/inflections.rb")
# The app's inflections, read (never run): only calls of acronym,
# irregular, uncountable, plural and singular with literal string arguments.
INFLECTION_CALLS = %i[acronym irregular uncountable plural singular].freeze
def inflection_calls(node, out)
  return unless node
  if node.is_a?(Prism::CallNode) && INFLECTION_CALLS.include?(node.name) && node.receiver.is_a?(Prism::LocalVariableReadNode)
    args = node.arguments&.arguments || []
    out << [node.name, args.map(&:unescaped)] if args.all? { |a| a.is_a?(Prism::StringNode) }
  end
  node.compact_child_nodes.each { |c| inflection_calls(c, out) }
end
if File.exist?(inflections)
  calls = []
  inflection_calls(Prism.parse_file(inflections).value, calls)
  ActiveSupport::Inflector.inflections(:en) do |inflect|
    calls.each { |name, args| inflect.public_send(name, *args) }
  end
end

Decl = Struct.new(:name, :kind, :superclass, :nesting, :file, :line, :table_name, :abstract, :prefix, :files, :unknown)
decls = {}

def const_name(node)
  case node
  when Prism::ConstantReadNode then node.name.to_s
  when Prism::ConstantPathNode
    last = node.respond_to?(:name) ? node.name : node.child.name
    node.parent ? "#{const_name(node.parent)}::#{last}" : "::#{last}"
  end
end

def literal(node)
  node.is_a?(Prism::StringNode) || node.is_a?(Prism::SymbolNode) ? node.unescaped : nil
end

def walk(node, nesting, file, decls)
  return unless node
  case node
  when Prism::ModuleNode, Prism::ClassNode
    name = const_name(node.constant_path)
    full = name.start_with?("::") ? name[2..] : [*nesting.last, name].compact.join("::")
    kind = node.is_a?(Prism::ClassNode) ? :class : :module
    d = (decls[full] ||= Decl.new(full, kind, nil, nesting.dup, file, node.location.start_line, nil, nil, nil, []))
    d.files << file unless d.files.include?(file)
    d.kind = :class if kind == :class
    if kind == :class && node.superclass
      d.superclass ||= node.superclass.slice
      d.nesting = nesting.dup
      d.file, d.line = file, node.location.start_line
    end
    node.body&.compact_child_nodes&.each { |c| body(c, d, nesting + [full], file, decls) }
    return
  end
  node.compact_child_nodes.each { |c| walk(c, nesting, file, decls) }
end

def body(node, d, nesting, file, decls)
  if node.is_a?(Prism::CallNode) && node.receiver.is_a?(Prism::SelfNode)
    arg = node.arguments&.arguments&.first
    if node.name == :table_name=
      literal(arg) ? d.table_name = literal(arg) : d.unknown ||= "table_name_not_literal"
    elsif node.name == :table_name_prefix=
      literal(arg) ? d.prefix = literal(arg) : d.unknown ||= "prefix_not_literal"
    end
    d.abstract = true if node.name == :abstract_class= && arg.is_a?(Prism::TrueNode)
  elsif node.is_a?(Prism::CallNode) && node.receiver.nil? && node.name == :primary_abstract_class
    d.abstract = true
  elsif node.is_a?(Prism::DefNode) && node.receiver.is_a?(Prism::SelfNode) && %i[table_name table_name_prefix].include?(node.name)
    stmts = node.body&.body
    value = literal(stmts.last) if stmts && stmts.size == 1
    if node.name == :table_name
      value ? d.table_name = value : d.unknown ||= "table_name_not_literal"
    else
      value ? d.prefix = value : d.unknown ||= "prefix_not_literal"
    end
  elsif node.is_a?(Prism::StatementsNode)
    node.body.each { |c| body(c, d, nesting, file, decls) }
  else
    walk(node, nesting, file, decls)
  end
end

files = Dir.glob(File.join(root, "**/*.rb")).reject { |p| p.include?("/vendor/") || p.include?("/node_modules/") }.sort
files.each do |path|
  rel = path.delete_prefix(root.chomp("/") + "/")
  walk(Prism.parse_file(path).value, [], rel, decls)
end

def resolve(name, nesting, decls)
  return name[2..] if name.start_with?("::")
  nesting.reverse.each do |scope|
    candidate = "#{scope}::#{name}"
    return candidate if decls.key?(candidate)
  end
  name
end

built = { "ActiveRecord::Base" => AR_BASE }
building = {}
define = lambda do |full|
  return built[full] if built.key?(full)
  d = decls[full]
  return nil if d.nil? || building[full]
  building[full] = true
  parent_name = full.include?("::") ? full.rpartition("::").first : nil
  parent = Object
  if parent_name
    # Each enclosing name: the declared module or class, else a plain module.
    prefix = []
    parent_name.split("::").each do |segment|
      prefix << segment
      scope = prefix.join("::")
      declared = decls.key?(scope) ? define.(scope) : nil
      parent =
        if declared.is_a?(Module) then declared
        elsif parent.const_defined?(segment, false) then parent.const_get(segment, false)
        else parent.const_set(segment, Module.new)
        end
    end
  end
  short = full.split("::").last
  if parent.const_defined?(short, false)
    # A library constant the app reopens (ActiveRecord, Rails): keep it.
    built[full] = parent.const_get(short, false)
    return built[full]
  end
  klass =
    if d.kind == :class && d.superclass
      sup_name = resolve(d.superclass, d.nesting, decls)
      sup = sup_name == "ActiveRecord::Base" ? AR_BASE : define.(sup_name)
      sup.is_a?(Class) && sup <= AR_BASE ? Class.new(sup) : Class.new
    elsif d.kind == :class
      Class.new
    else
      Module.new
    end
  parent.const_set(short, klass)
  if klass.is_a?(Class) && klass < AR_BASE
    klass.abstract_class = true if d.abstract
    klass.table_name = d.table_name if d.table_name
  end
  if d.prefix
    prefix = d.prefix
    if klass.is_a?(Class) && klass < AR_BASE
      klass.table_name_prefix = prefix
    else
      klass.define_singleton_method(:table_name_prefix) { prefix }
    end
  end
  built[full] = klass
end
decls.keys.sort_by { |k| k.count(":") }.each { |k| define.(k) }

def test_file?(file)
  file.split("/")[0...-1].any? { |part| %w[spec test].include?(part) }
end

# A model whose own or an ancestor's (class or enclosing module) table name
# or prefix is computed: ActiveRecord's answer here would be a guess.
def unknown(klass, decls)
  [*klass.ancestors.take_while { |a| a != AR_BASE }, *klass.module_parents].each do |a|
    reason = a.name && decls[a.name]&.unknown
    return reason if reason
  end
  nil
end

models, not_counted = [], []
decls.values.sort_by(&:name).each do |d|
  klass = built[d.name]
  next unless klass.is_a?(Class) && klass < AR_BASE && !klass.abstract_class?
  files = d.files.sort
  reason = unknown(klass, decls) || (files.all? { |f| test_file?(f) } ? "test_only" : nil)
  if reason
    not_counted << { "class" => d.name, "files" => files, "reason" => reason }
  else
    models << { "class" => d.name, "files" => files, "table" => klass.table_name }
  end
end
puts JSON.pretty_generate({ "oracle" => "ActiveRecord #{ActiveRecord.version} on Ruby #{RUBY_VERSION} (Prism)",
                            "models" => models, "not_counted" => not_counted })
