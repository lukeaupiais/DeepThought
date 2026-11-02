# SPDX-License-Identifier: Apache-2.0


def build():
    def parse():
        return "inner"
    return parse


class Reader:
    def parse(self):
        return "reader"


class Writer:
    def parse(self):
        return "writer"


async def consume():
    return None


TEXT = "def imaginary(): pass"
# def commented_out(): pass
