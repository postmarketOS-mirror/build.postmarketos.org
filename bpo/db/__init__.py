# Copyright 2020 Oliver Smith
# SPDX-License-Identifier: AGPL-3.0-or-later

""" Database code, using sqlalchemy ORM.
    Usage example:
        session = bpo.db.session()
        log = bpo.db.Log(action="db_init", details="hello world")
        session.add(log)
        session.commit() """

import enum
import sys
import json
import logging

import sqlalchemy
import sqlalchemy.orm
import sqlalchemy.ext.declarative
import sqlalchemy.sql
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, \
    Table, Index, Enum
from sqlalchemy.orm import relationship

import bpo.config.args
import bpo.db.migrate


base = sqlalchemy.ext.declarative.declarative_base()
session = None
engine = None
init_relationships_complete = False


class PackageStatus(enum.Enum):
    queued = 0
    building = 1
    built = 2
    published = 3
    failed = 4


class Package(base):
    __tablename__ = "package"

    # === Layout v0 === (only change in bpo.db.migrate.upgrade(), not here!)
    id = Column(Integer, primary_key=True)
    date = Column(DateTime(timezone=True),
                  server_default=sqlalchemy.sql.func.now())
    last_update = Column(DateTime(timezone=True),
                         onupdate=sqlalchemy.sql.func.now())
    arch = Column(String)
    branch = Column(String)
    pkgname = Column(String)
    status = Column(Enum(PackageStatus))
    job_id = Column(Integer, unique=True)

    # The following columns represent the latest state. We don't store the
    # history in bpo (avoids complexity, we have the git history for that).
    version = Column(String)
    repo = Column(String)
    # Package.depends: see init_relationships() below.

    Index("pkgname-arch-branch", pkgname, arch, branch, unique=True)
    Index("job_id", job_id)
    # [v1]: Index("arch-branch", Package.arch, Package.branch)
    # [v3]: Index("status", Package.status)
    # === End of layout v0 ===

    def __init__(self, arch, branch, pkgname, version):
        self.arch = arch
        self.branch = branch
        self.pkgname = pkgname
        self.version = version
        self.status = PackageStatus.queued

    def __repr__(self):
        depends = []
        for depend in self.depends:
            depends.append(depend.pkgname)
        return "{}/{}/{}-{}.apk (pmOS depends: {})".format(self.branch,
                                                           self.arch,
                                                           self.pkgname,
                                                           self.version,
                                                           depends)

    def depends_built(self):
        for depend in self.depends:
            if depend.status not in [PackageStatus.built,
                                     PackageStatus.published]:
                return False
        return True

    def depends_missing_list(self):
        ret = []
        for depend in self.depends:
            if depend.status not in [PackageStatus.built,
                                     PackageStatus.published]:
                ret += [depend]
        return ret


class Log(base):
    __tablename__ = "log"

    # === Layout v0 === (only change in bpo.db.migrate.upgrade(), not here!)
    id = Column(Integer, primary_key=True)
    date = Column(DateTime(timezone=True),
                  server_default=sqlalchemy.sql.func.now())
    action = Column(String)
    payload = Column(Text)
    arch = Column(String)
    branch = Column(String)
    pkgname = Column(String)
    version = Column(String)
    job_id = Column(Integer)
    # [v2]: commit = Column(String)
    # === End of layout v0 ===

    def __init__(self, action, payload=None, arch=None, branch=None,
                 pkgname=None, version=None, job_id=None):
        self.action = action
        self.payload = json.dumps(payload, indent=4) if payload else None
        self.arch = arch
        self.branch = branch
        self.pkgname = pkgname
        self.version = version
        self.job_id = job_id
        logging.info("### " + str(self) + " ###")

    def __repr__(self):
        ret = self.action
        if self.branch:
            ret += " " + self.branch + "/"
        if self.arch:
            ret += self.arch + "/"
        if self.pkgname:
            ret += self.pkgname
            if self.version:
                ret += "-" + self.version
        if self.job_id:
            ret += ", job: " + str(self.job_id)
        return ret


def init_relationships():
    # Only run this once!
    self = sys.modules[__name__]
    if self.init_relationships_complete:
        return
    self.init_relationships_complete = True

    # === Layout v0 === (only change in bpo.db.migrate.upgrade(), not here!)
    # package.depends - n:n - package.required_by
    # See "Self-Referential Many-to-Many Relationship" in:
    # https://docs.sqlalchemy.org/en/13/orm/join_conditions.html
    assoc = Table("package_dependency", base.metadata,
                  Column("package_id", ForeignKey("package.id"),
                         primary_key=True),
                  Column("dependency_id", ForeignKey("package.id"),
                         primary_key=True))
    primaryjoin = self.Package.id == assoc.c.package_id
    secondaryjoin = self.Package.id == assoc.c.dependency_id
    self.Package.depends = relationship("Package", secondary=assoc,
                                        primaryjoin=primaryjoin,
                                        secondaryjoin=secondaryjoin,
                                        order_by=self.Package.id,
                                        backref="required_by")
    # === End of layout v0 ===


def init():
    """ Initialize db """
    # Disable check_same_thread, so pysqlite does not print ProgrammingError
    # junk when running the tests with pytest. SQLAlchemy uses pooling to make
    # sure that a single connection is not used in more than one thread, so we
    # can safely disable this check.
    # https://docs.sqlalchemy.org/en/latest/dialects/sqlite.html
    connect_args = {"check_same_thread": False}

    self = sys.modules[__name__]
    url = "sqlite:///" + bpo.config.args.db_path

    # Open database, upgrade, close, open again
    for before_upgrade in [True, False]:
        self.engine = sqlalchemy.create_engine(url, connect_args=connect_args)
        init_relationships()
        self.base.metadata.create_all(engine)
        self.session = sqlalchemy.orm.sessionmaker(bind=engine)
        if before_upgrade:
            bpo.db.migrate.upgrade()
            self.engine.dispose()


def get_package(session, pkgname, arch, branch):
    result = session.query(bpo.db.Package).filter_by(arch=arch,
                                                     branch=branch,
                                                     pkgname=pkgname).all()
    return result[0] if len(result) else None


def get_all_packages_by_status(session):
    """ :returns: {"failed": pkglist1, "building": pkglist2, ...},
                  pkglist is a list of bpo.db.Package objects """
    ret = {}
    for status in bpo.db.PackageStatus:
        ret[status.name] = session.query(bpo.db.Package).\
            filter_by(status=status)
    return ret


def set_package_status(session, package, status, job_id=None):
    """ :param package: bpo.db.Package object
        :param status: bpo.db.PackageStatus value """
    package.status = status
    if job_id:
        package.job_id = job_id
    session.merge(package)
    session.commit()


def package_has_version(session, pkgname, arch, branch, version):
    count = session.query(bpo.db.Package).filter_by(arch=arch,
                                                    branch=branch,
                                                    pkgname=pkgname,
                                                    version=version).count()
    return True if count else False
