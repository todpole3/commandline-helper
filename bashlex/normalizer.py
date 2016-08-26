#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
This file augments the AST generated by bashlex with single-command structure.
It also performs some normalization on the command arguments.
"""

from __future__ import print_function
import copy
import os
import re
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "grammar"))

# bashlex stuff
import bast, errors, tokenizer, bparser
import bash
from lookup import ManPageLookUp

# TODO: add stdin & stdout types
simplified_bash_syntax = [
    "Command ::= SingleCommand | Pipe",
    "Pipe ::= Command '|' Command",
    "SingleCommand ::= HeadCommand [OptionList]",
    "OptionList ::= Option | OptionList",
    "Option ::= Flag [Argument] | LogicOp Option",
    "Argument ::= SingleArgument | CommandSubstitution | ProcessSubstitution",
    "CommandSubstitution ::= ` Command `",
    "ProcessSubstitution ::= <( Command ) | >( Command )"
]

arg_syntax = [
    "File",
    "Pattern",
    "Number",
    "NumberExp ::= -Number | +Number",
    "SizeExp ::= Number(k) | Number(M) | Number(G) | Number(T) | Number(P)",
    "TimeExp ::= Number(s) | Number(m) | Number(h) | Number(d) | Number(w)",
    # TODO: add fine-grained permission pattern
    "PermissionMode",
    "UserName",
    "GroupName",
    "Unknown"
]

unary_logic_operators = set(['!', '-not'])

binary_logic_operators = set([
    '-and',
    '-or',
    '||',
    '&&',
    '-o',
    '-a'
])

man_lookup = ManPageLookUp([os.path.join(os.path.dirname(__file__), "..", "grammar",
                                         "primitive_cmds_grammar.json")])

def is_unary_logic_op(node, parent):
    if node.word == "!":
        return parent and parent.kind == "headcommand" and parent.value == "find"
    return node.word in unary_logic_operators

def is_binary_logic_op(node, parent):
    if node.word == '-o':
        if parent and parent.kind == "headcommand" and parent.value == "find":
            node.word = "-or"
            return True
        else:
            return False
    if node.word == '-a':
        if parent and parent.kind == "headcommand" and parent.value == "find":
            node.word = "-and"
            return True
        else:
            return False
    return node.word in binary_logic_operators

def all_simple_commands(ast):
    """Check if an ast contains only high-frequency commands."""
    node = ast
    if node.kind == "headcommand" and not node.value in bash.head_commands:
        return False
    for child in node.children:
        if not all_simple_commands(child):
            return False
    return True

class Node(object):
    num_child = -1      # default value = -1, allow arbitrary number of children
    children_types = [] # list of children types
                        # a length-one list of representing the common types for each
                        # child if num_child = -1
                        # dummy field if num_child = 0

    def __init__(self, parent=None, lsb=None, kind="", value=""):
        """
        :member kind: ['pipe',
                      'headcommand',
                      'logicop',
                      'flag',
                      'file', 'pattern', 'numberexp',
                      'sizeexp', 'timeexp', 'permexp',
                      'username', 'groupname', 'unknown',
                      'number', 'unit', 'op',
                      'commandsubstitution',
                      'processsubstitution'
                     ]
        :member value: string value of the node
        :member parent: pointer to parent node
        :member lsb: pointer to left sibling node
        :member children: list of child nodes
        """
        self.parent = parent
        self.lsb = lsb
        self.rsb = None
        self.kind = kind
        self.value = value
        self.children = []

    def addChild(self, child):
        self.children.append(child)

    def getNumChildren(self):
        return len(self.children)

    def getLeftChild(self):
        if len(self.children) >= 1:
            return self.children[0]
        else:
            return None

    def getRightChild(self):
        if len(self.children) >= 1:
            return self.children[-1]
        else:
            return None

    def getSecond2RightChild(self):
        if len(self.children) >= 2:
            return self.children[-2]
        else:
            return None

    def grandparent(self):
        return self.parent.parent

    def removeChild(self, child):
        self.children.remove(child)

    def removeChildByIndex(self, index):
        self.children.pop(index)

    def replaceChild(self, child, new_child):
        new_child.parent = child.parent
        index = self.children.index(child)
        self.removeChild(child)
        self.children.insert(index, new_child)
        make_sibling(child.lsb, new_child)
        make_sibling(new_child, child.rsb)

    def substituteParentheses(self, lp, rp, new_child):
        # substitute parenthese expression with single node
        assert(lp.parent == rp.parent)
        new_child.parent = rp.parent
        make_sibling(lp.lsb, new_child)
        make_sibling(new_child, rp.rsb)
        index = self.children.index(lp)
        self.removeChild(lp)
        self.removeChild(rp)
        self.children.insert(index, new_child)
        return index

    @property
    def symbol(self):
        return self.kind.upper() + "_" + self.value

# syntax constraints for different kind of nodes
class ArgumentNode(Node):
    num_child = 0

    def __init__(self, kind="", value="", arg_type="", parent=None, lsb=None):
        super(ArgumentNode, self).__init__(parent, lsb, kind, value)
        self.arg_type = arg_type
        # print(self.arg_type)

    def getHeadCommand(self):
        if self.kind == "headcommand":
            return self
        if self.kind == "flag" or self.kind == "argument":
            ancester = self.parent
            while (ancester.kind != "headcommand"):
                ancester = ancester.parent
            return ancester

class UnaryLogicOpNode(Node):
    num_child = 1
    children_types = [set('flag')]

    def __init__(self, value="", parent=None, lsb=None):
        super(UnaryLogicOpNode, self).__init__( parent, lsb, 'unarylogicop', value)

class BinaryLogicOpNode(Node):
    num_child = 2
    children_types = [set('flag'), set('flag')]

    def __init__(self, value="", parent=None, lsb=None):
        super(BinaryLogicOpNode, self).__init__(parent, lsb, 'binarylogicop', value)

class PipelineNode(Node):
    children_types = [set(['headcommand'])]

    def __init__(self, parent=None, lsb=None):
        super(PipelineNode, self).__init__(parent, lsb)
        self.kind = 'pipeline'

class CommandSubstitutionNode(Node):
    num_child = 1
    children_types = [set(['pipe', 'headcommand'])]

    def __init__(self, parent=None, lsb=None):
        super(CommandSubstitutionNode, self).__init__(parent, lsb)
        self.kind = "commandsubstitution"

class ProcessSubstitutionNode(Node):
    num_child = 1
    children_types = [set(['pipe', 'headcommand'])]

    def __init__(self, value, parent=None, lsb=None):
        super(ProcessSubstitutionNode, self).__init__(parent, lsb)
        self.kind = "processsubstitution"
        if value in ["<", ">"]:
            self.value = value
        else:
            raise ValueError("Value of a processsubstitution has to be '<' or '>'.")

def pretty_print(node, depth=0):
    print("    " * depth + node.kind.upper() + '(' + node.value + ')')
    for child in node.children:
        pretty_print(child, depth+1)

def to_list(node, order='dfs', list=None):
    # linearize the tree for training
    if order == 'dfs':
        list.append(node.symbol)
        for child in node.children:
            to_list(child, order, list)
        list.append("<NO_EXPAND>")
    return list

def to_tokens(node, loose_constraints=False, ignore_flag_order=False,
              arg_type_only=False):
    """convert a bash AST to a list of tokens"""

    if not node:
        return []

    lc = loose_constraints
    ifo = ignore_flag_order
    ato = arg_type_only

    def to_tokens_fun(node):
        tokens = []
        if node.kind == "root":
            try:
                assert(loose_constraints or node.getNumChildren() == 1)
            except AssertionError, e:
                return []
            if lc:
                for child in node.children:
                    tokens += to_tokens_fun(child)
            else:
                tokens = to_tokens_fun(node.children[0])
        elif node.kind == "pipeline":
            assert(loose_constraints or node.getNumChildren() > 1)
            if lc and node.getNumChildren() < 1:
                tokens.append("|")
            elif lc and node.getNumChildren() == 1:
                # treat "single-pipe" as atomic command
                tokens += to_tokens_fun(node.children[0])
            else:
                for child in node.children[:-1]:
                    tokens += to_tokens_fun(child)
                    tokens.append("|")
                tokens += to_tokens_fun(node.children[-1])
        elif node.kind == "commandsubstitution":
            assert(loose_constraints or node.getNumChildren() == 1)
            if lc and node.getNumChildren() < 1:
                tokens += ["$(", ")"]
            else:
                tokens.append("$(")
                tokens += to_tokens_fun(node.children[0])
                tokens.append(")")
        elif node.kind == "processsubstitution":
            assert(loose_constraints or node.getNumChildren() == 1)
            if lc and node.getNumChildren() < 1:
                tokens.append(node.value + "(")
                tokens.append(")")
            else:
                tokens.append(node.value + "(")
                tokens += to_tokens_fun(node.children[0])
                tokens.append(")")
        elif node.kind == "headcommand":
            tokens.append(node.value)
            children = sorted(node.children, key=lambda x:x.value) if ifo else node.children
            for child in children:
                tokens += to_tokens_fun(child)
        elif node.kind == "flag":
            if '::' in node.value:
                value, op = node.value.split('::')
                tokens.append(value)
            else:
                tokens.append(node.value)
            for child in node.children:
                tokens += to_tokens_fun(child)
            if '::' in node.value:
                tokens.append(op)
        elif node.kind == "binarylogicop":
            assert(loose_constraints or node.getNumChildren() > 1)
            if lc and node.getNumChildren() < 2:
                for child in node.children:
                    tokens += to_tokens_fun(child)
            else:
                tokens.append("\\(")
                for i in xrange(len(node.children)-1):
                    tokens += to_tokens_fun(node.children[i])
                    tokens.append(node.value)
                tokens += to_tokens_fun(node.children[-1])
                tokens.append("\\)")
        elif node.kind == "unarylogicop":
            assert(loose_constraints or node.getNumChildren() == 1)
            if lc and node.getNumChildren() < 1:
                tokens.append(node.value)
            else:
                tokens.append(node.value)
                tokens += to_tokens_fun(node.children[0])
        elif node.kind == "argument":
            assert(loose_constraints or node.getNumChildren() == 0)
            if ato and not node.arg_type == "ReservedWord":
                tokens.append(node.arg_type)
            else:
                tokens.append(node.value)
            if lc:
                for child in node.children:
                    tokens += to_tokens_fun(child)
        return tokens

    return to_tokens_fun(node)

def to_template(node, loose_constraints=False):
    # convert a bash AST to a template that contains only reserved words and argument types
    # flags are ordered alphabetically
    tokens = to_tokens(node, loose_constraints, ignore_flag_order=True,
                       arg_type_only=True)
    return ' '.join(tokens)

def to_command(node, loose_constraints=False, ignore_flag_order=False):
    return ' '.join(to_tokens(node, loose_constraints, ignore_flag_order))

def to_ast(list, order='dfs'):
    # construct a tree from linearized input
    root = Node(kind="root", value="root")
    current = root
    if order == 'dfs':
        for i in xrange(1, len(list)):
            if not current:
                break
            symbol = list[i]
            if symbol == "<NO_EXPAND>":
                current = current.parent
            else:
                kind, value = symbol.split('_', 1)
                kind = kind.lower()
                # add argument types
                if kind == "argument":
                    if current.kind == "flag":
                        head_cmd = current.getHeadCommand().value
                        flag = current.value
                        arg_type = man_lookup.get_flag_arg_type(head_cmd, flag)
                    elif current.kind == "headcommand":
                        head_cmd = current.value
                        arg_type = type_check(value, man_lookup.get_arg_types(head_cmd))
                    else:
                        print("Warning: to_ast unrecognized argument attachment point {}.".format(current.symbol))
                        arg_type = "Unknown"
                    node = ArgumentNode(kind=kind, value=value, arg_type=arg_type)
                elif kind == "flag" or kind == "headcommand":
                    node = ArgumentNode(kind=kind, value=value)
                else:
                    node = Node(kind=kind, value=value)
                attach_to_tree(node, current)
                current = node
    else:
        raise NotImplementedError
    return root

def special_command_normalization(cmd):
    # special normalization for certain commands
    ## remove all "sudo"'s
    cmd = cmd.replace("sudo", "")

    ## normalize utilities called with full path
    cmd = cmd.replace("/usr/bin/find", "find")
    cmd = cmd.replace("~/bin/find", "find")
    cmd = cmd.replace("/bin/find", "find")

    ## remove shell character
    if cmd.startswith("\$ "):
        cmd = re.sub("^\$ ", '', cmd)
    if cmd.startswith("\# "):
        cmd = re.sub("^\# ", '', cmd)
    if cmd.startswith("\$find "):
        cmd = re.sub("^\$find ", "find ", cmd)
    if cmd.startswith("\#find "):
        cmd = re.sub("^\#find ", "find ", cmd)

    ## correct common spelling errors
    cmd = cmd.replace("-\\(", "\\(")
    cmd = cmd.replace("-\\)", "\\)")
    cmd = cmd.replace("\"\\)", " \\)")

    ## the first argument of "tar" is always interpreted as an option
    tar_fix = re.compile(' tar \w')
    if cmd.startswith('tar'):
        cmd = ' ' + cmd
        for w in re.findall(tar_fix, cmd):
            cmd = cmd.replace(w, w.replace('tar ', 'tar -'))
        cmd = cmd.strip()
    return cmd

def attach_to_tree(node, parent):
    node.parent = parent
    node.lsb = parent.getRightChild()
    parent.addChild(node)
    if node.lsb:
        node.lsb.rsb = node

def make_parentchild(parent, child):
    parent.addChild(child)
    child.parent = parent

def make_sibling(lsb, rsb):
    if lsb:
        lsb.rsb = rsb
    if rsb:
        rsb.lsb = lsb

def type_check(word, possible_types):
    """Heuristically determine argument types."""
    if word in ["+", ";", "{}"]:
        return "ReservedWord"
    if word.isdigit() and "Number" in possible_types:
        return "Number"
    elif any(c.isdigit() for c in word):
        if word[-1] in ["k", "M", "G", "T", "P"] and "Size" in possible_types:
            return "Size"
        if word[-1] in ["s", "m", "h", "d", "w"] and "Time" in possible_types:
            return "Time"
    elif "File" in possible_types:
        return "File"
    elif "Pattern" in possible_types:
        return "Pattern"
    elif "Utility" in possible_types:
        # TODO: this argument type is not well-handled
        # This is usuallly third-party utitlies
        return "Utility"
    else:
        raise ValueError("Unable to decide type for {}".format(word))

def normalize_ast(cmd, normalize_digits=True, normalize_long_pattern=True,
                  recover_quotation=True):
    """
    Convert the bashlex parse tree of a command into the normalized form.
    :param cmd: bash command to parse
    :param normalize_digits: replace all digits in the tree with the special _NUM symbol
    :param recover_quotation: if set, retain quotation marks in the command
    :return normalized_tree
    """
    print(cmd.encode('utf-8'))
    cmd = cmd.replace('\n', ' ').strip()
    cmd = special_command_normalization(cmd)

    if not cmd:
        return None

    def normalize_word(node, kind, norm_digit, norm_long_pattern, recover_quote):
        w = recover_quotation(node) if recover_quote else node.word
        if kind == "argument":
            if ' ' in w:
                try:
                    assert(w.startswith('"') and w.endswith('"'))
                except AssertionError, e:
                    print("Quotation Error: space inside word " + w)
                if norm_long_pattern:
                    w = bash._LONG_PATTERN
            if norm_digit:
                w = re.sub(bash._DIGIT_RE, bash._NUM, w)
        return w

    def recover_quotation(node):
        if with_quotation(node):
            return cmd[node.pos[0] : node.pos[1]]
        else:
            return node.word

    def with_quotation(node):
        return cmd[node.pos[0]] == '"' or cmd[node.pos[1]-1] == '"'

    def normalize_command(node, current):
        attach_point = current

        END_OF_OPTIONS = False

        head_commands = []
        unary_logic_ops = []
        binary_logic_ops = []
        unprocessed_unary_logic_ops = []
        unprocessed_binary_logic_ops = []

        def attach_option(node, attach_point):
            attach_point = find_flag_attach_point(node, attach_point)
            if bash.is_double_option(node.word) or node.word in unary_logic_operators \
                    or node.word in binary_logic_operators or attach_point.value == "find"\
                    or len(node.word) <= 1:
                normalize(node, attach_point, "flag")
            else:
                # split flags
                assert(node.word.startswith('-'))
                options = node.word[1:]
                if len(options) == 1:
                    normalize(node, attach_point, "flag")
                else:
                    str = options + " splitted into: "
                    for option in options:
                        new_node = copy.deepcopy(node)
                        new_node.word = '-' + option
                        normalize(new_node, attach_point, "flag")
                        str += new_node.word + ' '
                    print(str)

            attach_point = attach_point.getRightChild()
            return attach_point

        def attach_argument(node, attach_point):
            if attach_point.kind == "flag" and attach_point.getNumChildren() >= 1:
                attach_point = attach_point.parent

            if attach_point.kind == "flag":
                # attach point is flag of some headcommand
                head_cmd = attach_point.getHeadCommand()
                flag = attach_point.value
                arg_type = man_lookup.get_flag_arg_type(head_cmd.value, flag)
                if not arg_type:
                    # attach point flag does not take argument
                    attach_point = attach_point.getHeadCommand()
            elif attach_point.kind == "headcommand":
                head_cmd = attach_point.value
                possible_arg_types = man_lookup.get_arg_types(head_cmd)
                arg_type = type_check(node.word, possible_arg_types)
            else:
                # TODO: this exceptional case is not handled very well
                # most likely due to assignment node
                print("Warning: attach_argument - nrecognized argument attachment point kind: {}"
                      .format(attach_point.kind))
                arg_type = "Unknown"
            normalize(node, attach_point, "argument", arg_type)

        def fail_headcommand_attachment_check(err_msg, attach_point, child):
            msg_head = "Error attaching headcommand: "
            print(msg_head + err_msg)
            print(attach_point.symbol)
            print(child)
            # raise HeadCommandAttachmentError(msg_head + err_msg)

        def find_flag_attach_point(node, attach_point):
            if attach_point.kind == "flag":
                return find_flag_attach_point(node, attach_point.parent)
            elif attach_point.kind == "headcommand":
                return attach_point
            elif attach_point.kind == "unarylogicop" or \
                attach_point.kind == "binarylogicop":
                return attach_point
            else:
                raise ValueError("Error: cannot decide where to attach flag node")

        def organize_buffer(buffer):
            norm_node = BinaryLogicOpNode(value="-and")
            for node in buffer:
                attach_to_tree(node, norm_node)
            return norm_node

        def adjust_unary_operators(node):
            # change right sibling to child
            rsb = node.rsb
            if not rsb:
                print("Warning: unary logic operator without a right sibling.")
                print(node.parent)
                return

            if rsb.value == "(":
                unprocessed_unary_logic_ops.append(node)
                return

            make_sibling(node, rsb.rsb)
            node.parent.removeChild(rsb)
            rsb.parent = node
            rsb.lsb = None
            rsb.rsb = None
            node.addChild(rsb)

        def adjust_binary_operators(node):
            # change right sibling to Child
            # change left sibling to child
            rsb = node.rsb
            lsb = node.lsb

            if not rsb or not lsb:
                print("Error: binary logic operator must have both left and right siblings.")
                print(node.parent)
                sys.exit()

            if rsb.value == "(" or lsb.value == ")":
                unprocessed_binary_logic_ops.append(node)
                # sibling is parenthese
                return

            assert(rsb.value != ")")
            assert(lsb.value != "(")

            make_sibling(node, rsb.rsb)
            make_sibling(lsb.lsb, node)
            node.parent.removeChild(rsb)
            node.parent.removeChild(lsb)
            rsb.rsb = None
            lsb.lsb = None

            if lsb.kind == "binarylogicop" and lsb.value == node.value:
                for lsbc in lsb.children:
                    make_parentchild(node, lsbc)
                make_parentchild(node, rsb)
                lsbcr = lsb.getRightChild()
                make_sibling(lsbcr, rsb)
            else:
                make_parentchild(node, lsb)
                make_parentchild(node, rsb)
                make_sibling(lsb, rsb)

            # resolve single child of binary operators left as the result of parentheses processing
            if node.parent.kind == "binarylogicop" and node.parent.value == "-and":
                if node.parent.getNumChildren() == 1:
                    node.grandparent().replaceChild(node.parent, node)

        # normalize atomic command
        parentheses_attach_points = []

        i = 0
        while i < len(node.parts):
            child = node.parts[i]
            if child.kind == 'word':
                if child.word == "--":
                    END_OF_OPTIONS = True

                elif child.word in unary_logic_operators:
                    attach_point = find_flag_attach_point(child, attach_point)
                    if is_unary_logic_op(child, attach_point):
                        norm_node = UnaryLogicOpNode(child.word)
                        attach_to_tree(norm_node, attach_point)
                        unary_logic_ops.append(norm_node)
                    else:
                        attach_point = attach_option(child, attach_point)

                elif child.word in binary_logic_operators:
                    attach_point = find_flag_attach_point(child, attach_point)
                    if is_binary_logic_op(child, attach_point):
                        norm_node = BinaryLogicOpNode(child.word)
                        attach_to_tree(norm_node, attach_point)
                        binary_logic_ops.append(norm_node)
                    else:
                        attach_point = attach_option(child, attach_point)

                elif bash.is_headcommand(child.word) and not with_quotation(child) and \
                    (attach_point.kind != "headcommand" or attach_point.value in
                        ["sh", "csh", "ksh", "tcsh", "zsh", "bash", "exec", "xargs"]):
                    if i > 0:
                        # embedded commands
                        if attach_point.kind == "flag":
                            if attach_point.value in ["-exec", "-execdir", "-ok", "-okdir"]:
                                new_command_node = copy.deepcopy(node)
                                new_command_node.parts = []
                                subcommand_added = False
                                for j in xrange(i, len(node.parts)):
                                    if not hasattr(node.parts[j], 'word'):
                                        # TODO: this exceptional case is not handled very well
                                        # most likely due to a redirection node
                                        continue
                                    elif node.parts[j].word == ";" or\
                                       node.parts[j].word == "+":
                                        normalize_command(new_command_node, attach_point)
                                        attach_point.value += '::' + node.parts[j].word
                                        subcommand_added = True
                                        break
                                    else:
                                        new_command_node.parts.append(node.parts[j])
                                if not subcommand_added:
                                    print("Warning: -exec missing ending ';'")
                                    normalize_command(new_command_node, attach_point)
                                    new_node = copy.deepcopy(node.parts[-1])
                                    new_node.word = "\\;"
                                    normalize(new_node, attach_point, "argument")
                                i = j
                                # handle end of utility introduced by '-exec' and whatnots
                                attach_point = attach_point.parent
                            else:
                                # TODO: this exeptional case is not handled very well
                                # since attachment point flag does not take utility arguments, the token
                                # is likely to be a normal argument
                                attach_argument(child, attach_point)
                        elif attach_point.kind == "headcommand":
                            new_command_node = copy.deepcopy(node)
                            new_command_node.parts = node.parts[i:]
                            normalize_command(new_command_node, attach_point)
                            i = len(node.parts) - 1
                        else:
                            fail_headcommand_attachment_check(
                                "headcommand attached to argument",
                                attach_point, child)
                    else:
                        normalize(child, attach_point, "headcommand")
                        attach_point = attach_point.getRightChild()
                        head_commands.append(attach_point)

                elif child.word.startswith('-') and not END_OF_OPTIONS:
                    # check if child is a flag
                    if attach_point.kind == "flag" and any(c.isdigit() for c in child.word):
                        head_cmd = attach_point.getHeadCommand()
                        flag = attach_point.value
                        arg_type = man_lookup.get_flag_arg_type(head_cmd.value, flag)
                        if arg_type and attach_point.getNumChildren() == 0:
                            # child is an argument starts with a minus symbol
                            normalize(child, attach_point, "argument", arg_type)
                        else:
                            # child is a flag
                            attach_point = attach_option(child, attach_point)
                    else:
                        attach_point = attach_option(child, attach_point)

                else:
                    if child.word == "(":
                        parentheses_attach_points.append(attach_point)
                    if child.word == ")":
                        attach_point = parentheses_attach_points.pop()
                    attach_argument(child, attach_point)

            elif child.kind == "assignment":
                normalize(child, attach_point, "assignment")
            else:
                # TODO: this corner case is not handled very well
                # usually caused by "redirect" node
                normalize(child, attach_point)

            i += 1

        assert(len(parentheses_attach_points) == 0)

        # TODO: some commands get parsed with no head command
        # This is usually due to utilities unrecognized by us, e.g. "gen_root.sh".
        if len(head_commands) == 0:
            return

        if len(head_commands) > 1:
            print("Error: multiple headcommands in one command.")
            for hc in head_commands:
                print(hc.symbol)
            sys.exit()

        head_command = head_commands[0]

        # process unary logic operators
        for ul in unary_logic_ops:
            adjust_unary_operators(ul)

        # process binary logic operators
        for bl in binary_logic_ops:
            adjust_binary_operators(bl)

        # process (embedded) parenthese -- treat as implicit "-and"
        stack = []
        buffer = []
        depth = 0

        i = 0
        while i < head_command.getNumChildren():
            child = head_command.children[i]
            if child.value == "(":
                stack.append(child)
                depth += 1
            elif child.value == ")":
                assert(depth >= 1)
                # popping pushed states off the stack
                popped = stack.pop()
                while (popped.value != "("):
                    buffer.insert(0, popped)
                    head_command.removeChild(popped)
                    popped = stack.pop()
                lparenth = popped
                rparenth = child
                new_child = organize_buffer(buffer) if len(buffer) > 1 else buffer[0]
                buffer = []
                i = head_command.substituteParentheses(lparenth, rparenth, new_child)
                depth -= 1
                if depth >= 1:
                    # embedded parenthese
                    stack.append(new_child)
            elif depth >= 1:
                stack.append(child)
            i += 1

        for ul in unprocessed_unary_logic_ops:
            adjust_unary_operators(ul)

        for bl in unprocessed_binary_logic_ops:
            adjust_binary_operators(bl)

        assert(len(stack) == 0)
        assert(depth == 0)

    def normalize(node, current, node_kind="", arg_type=""):
        # recursively normalize each subtree
        if not type(node) is bast.node:
            raise ValueError('type(node) is not ast.node')
        if node.kind == 'word':
            # assign fine-grained types
            if node.parts:
                # Compound arguments
                # commandsubstitution, processsubstitution, parameter
                if node.parts[0].kind == "processsubstitution":
                    if '>' in node.word:
                        norm_node = ProcessSubstitutionNode('>')
                        attach_to_tree(norm_node, current)
                        for child in node.parts:
                            normalize(child, norm_node)
                    elif '<' in node.word:
                        norm_node = ProcessSubstitutionNode('<')
                        attach_to_tree(norm_node, current)
                        for child in node.parts:
                            normalize(child, norm_node)
                elif node.parts[0].kind == "commandsubstitution":
                    norm_node = CommandSubstitutionNode()
                    attach_to_tree(norm_node, current)
                    for child in node.parts:
                        normalize(child, norm_node)
                elif node.parts[0].kind == "parameter" or \
                    node.parts[0].kind == "tilde":
                    value = normalize_word(node, "argument", normalize_digits, normalize_long_pattern,
                                           recover_quotation)
                    norm_node = ArgumentNode(kind=node_kind, value=value, arg_type=arg_type)
                    attach_to_tree(norm_node, current)
                else:
                    for child in node.parts:
                        normalize(child, current)
            else:
                value = normalize_word(node, node_kind, normalize_digits, normalize_long_pattern,
                                       recover_quotation)
                norm_node = ArgumentNode(kind=node_kind, value=value, arg_type=arg_type)
                attach_to_tree(norm_node, current)
        elif node.kind == "pipeline":
            norm_node = PipelineNode()
            attach_to_tree(norm_node, current)
            if len(node.parts) % 2 == 0:
                print("Error: pipeline node must have odd number of parts")
                print(node)
                sys.exit()
            for child in node.parts:
                if child.kind == "command":
                    normalize(child, norm_node)
                elif child.kind == "pipe":
                    pass
                else:
                    raise ValueError("Error: unrecognized type of child of pipeline node")
        elif node.kind == "list":
            if len(node.parts) > 2:
                # multiple commands, not supported
                raise ValueError("Unsupported: list of length >= 2")
            else:
                normalize(node.parts[0], current)
        elif node.kind == "commandsubstitution" or \
             node.kind == "processsubstitution":
            normalize(node.command, current)
        elif node.kind == "command":
            try:
                normalize_command(node, current)
            except AssertionError, e:
                raise AssertionError("normalized_command AssertionError")
        elif hasattr(node, 'parts'):
            for child in node.parts:
                # skip current node
                normalize(child, current)
        elif node.kind == "operator":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "parameter":
            # not supported
            raise ValueError("Unsupported: parameters")
        elif node.kind == "redirect":
            # not supported
            # if node.type == '>':
            #     parse(node.input, tokens)
            #     tokens.append('>')
            #     parse(node.output, tokens)
            # elif node.type == '<':
            #     parse(node.output, tokens)
            #     tokens.append('<')
            #     parse(node.input, tokens)
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "for":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "if":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "while":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "until":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "assignment":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "function":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "tilde":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "heredoc":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)

    try:
        tree = bparser.parse(cmd)
    except tokenizer.MatchedPairError, e:
        print("Cannot parse: %s - MatchedPairError" % cmd.encode('utf-8'))
        # return basic_tokenizer(cmd, normalize_digits, False)
        return None
    except errors.ParsingError, e:
        print("Cannot parse: %s - ParsingError" % cmd.encode('utf-8'))
        # return basic_tokenizer(cmd, normalize_digits, False)
        return None
    except NotImplementedError, e:
        print("Cannot parse: %s - NotImplementedError" % cmd.encode('utf-8'))
        # return basic_tokenizer(cmd, normalize_digits, False)
        return None
    except IndexError, e:
        print("Cannot parse: %s - IndexError" % cmd.encode('utf-8'))
        # empty command
        return None
    except AttributeError, e:
        print("Cannot parse: %s - AttributeError" % cmd.encode('utf-8'))
        # not a bash command
        return None

    if len(tree) > 1:
        print("Doesn't support command with multiple root nodes: %s" % cmd.encode('utf-8'))
    normalized_tree = Node(kind="root")
    try:
        normalize(tree[0], normalized_tree)
    except ValueError as err:
        print("%s - %s" % (err.args[0], cmd.encode('utf-8')))
        return None
    except AssertionError as err:
        print("%s - %s" % (err.args[0], cmd.encode('utf-8')))
        return None

    return normalized_tree

# --- Debugging ---

class HeadCommandAttachmentError(Exception):
    def __init__(self, message, errors=None):
        self.message = message
        self.errors = errors

if __name__ == "__main__":
    cmd = sys.argv[1]
    norm_tree = normalize_ast(cmd)
    pretty_print(norm_tree, 0)
    list = to_list(norm_tree, 'dfs', [])
    print(list)
    tree = to_ast(list + ['<PAD>'])
    pretty_print(tree, 0)
    print(to_template(tree))
    print(to_command(tree))
