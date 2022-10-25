#!/usr/bin/env python

from setuptools import setup

setup(name='cvl-3d-viz',
      version='1.0',
      packages=['cvl'],
      install_requires=[
          'requests',
          'numpy',
          'gdal'
          ]
      )
