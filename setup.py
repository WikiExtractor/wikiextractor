from setuptools import setup
import re

from scripts.WikiExtractor import version


def to_semantic_version(version):
    if re.match(r'^\d+\.\d+$', version):
        return version + '.0'
    return version

setup(
    name='wikiextractor',
    version=to_semantic_version(version),
    description='A tool for extracting plain text from Wikipedia dumps',
    packages=[
        'wikiextractor'
    ],
    install_requires=[
    ],
    tests_require=[
        'nose>=1.0',
    ],
    test_suite='nose.collector',
)
