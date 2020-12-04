from setuptools import setup, find_packages
import re

from wikiextractor.WikiExtractor import __version__


def get_version(version):
    if re.match(r'^\d+\.\d+$', version):
        return version + '.0'
    return version

with open("README.md", "r") as fh:
    long_description = fh.read()

setup(
    name='wikiextractor',
    version=get_version(__version__),
    author='Giuseppe Attardi',
    author_email='attardi@gmail.com',
    description='A tool for extracting plain text from Wikipedia dumps',
    long_description=long_description,
    long_description_content_type="text/markdown",
    license='GNU Affero General Public License',
    install_requires=[],
    url="https://github.com/attardi/wikiextractor",
    packages=find_packages(include=["wikiextractor"]),
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'Topic :: Text Processing :: Linguistic',
        'License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)',
        'Programming Language :: Python :: 3'
     ],
    entry_points={
        "console_scripts": [
            "wikiextractor = wikiextractor.WikiExtractor:main",
            "extractPage = wikiextractor.extractPage:main",
            ]
        },
    python_requires='>=3.6',
)
