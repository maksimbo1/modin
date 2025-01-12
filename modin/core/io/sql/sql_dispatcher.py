# Licensed to Modin Development Team under one or more contributor license agreements.
# See the NOTICE file distributed with this work for additional information regarding
# copyright ownership.  The Modin Development Team licenses this file to you under the
# Apache License, Version 2.0 (the "License"); you may not use this file except in
# compliance with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under
# the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific language
# governing permissions and limitations under the License.

"""
Module houses `SQLDispatcher` class.

`SQLDispatcher` contains utils for handling SQL queries or database tables,
inherits util functions for handling files from `FileDispatcher` class and can be
used as base class for dipatchers of SQL queries.
"""

import math
import numpy as np
import pandas
import warnings

from modin.core.io.file_dispatcher import FileDispatcher
from modin.config import NPartitions


class SQLDispatcher(FileDispatcher):
    """
    Class handles utils for reading SQL queries or database tables.

    Inherits some common for files util functions from `FileDispatcher` class.
    """

    @classmethod
    def _read(cls, sql, con, index_col=None, **kwargs):
        """
        Read a SQL query or database table into a query compiler.

        Parameters
        ----------
        sql : str or SQLAlchemy Selectable (select or text object)
            SQL query to be executed or a table name.
        con : SQLAlchemy connectable, str, or sqlite3 connection
            Connection object to database.
        index_col : str or list of str, optional
            Column(s) to set as index(MultiIndex).
        **kwargs : dict
            Parameters to pass into `pandas.read_sql` function.

        Returns
        -------
        BaseQueryCompiler
            Query compiler with imported data for further processing.
        """
        try:
            import psycopg2 as pg

            if isinstance(con, pg.extensions.connection):
                con = "postgresql+psycopg2://{}:{}@{}{}/{}".format(  # Table in DB
                    con.info.user,  # <Username>: for DB
                    con.info.password,  # Password for DB
                    con.info.host if con.info.host != "/tmp" else "",  # @<Hostname>
                    (":" + str(con.info.port))
                    if con.info.host != "/tmp"
                    else "",  # <port>
                    con.info.dbname,  # Table in DB
                )
        except ImportError:
            pass
        # In the case that we are given a SQLAlchemy Connection or Engine, the objects
        # are not pickleable. We have to convert it to the URL string and connect from
        # each of the workers.
        if not isinstance(con, str):
            warnings.warn(
                "To use parallel implementation of `read_sql`, pass the sqlalchemy"
                "connection string instead of {}.".format(type(con))
            )
            return cls.single_worker_read(sql, con=con, index_col=index_col, **kwargs)
        row_cnt_query = "SELECT COUNT(*) FROM ({}) as foo".format(sql)
        row_cnt = pandas.read_sql(row_cnt_query, con).squeeze()
        cols_names_df = pandas.read_sql(
            "SELECT * FROM ({}) as foo LIMIT 0".format(sql), con, index_col=index_col
        )
        cols_names = cols_names_df.columns
        num_partitions = NPartitions.get()
        partition_ids = []
        index_ids = []
        dtype_ids = []
        limit = math.ceil(row_cnt / num_partitions)
        for part in range(num_partitions):
            offset = part * limit
            query = "SELECT * FROM ({}) as foo LIMIT {} OFFSET {}".format(
                sql, limit, offset
            )
            partition_id = cls.deploy(
                cls.parse,
                num_partitions + 2,
                dict(
                    num_splits=num_partitions,
                    sql=query,
                    con=con,
                    index_col=index_col,
                    **kwargs,
                ),
            )
            partition_ids.append(
                [cls.frame_partition_cls(obj) for obj in partition_id[:-2]]
            )
            index_ids.append(partition_id[-2])
            dtype_ids.append(partition_ids[-1])
        if index_col is None:  # sum all lens returned from partitions
            index_lens = cls.materialize(index_ids)
            new_index = pandas.RangeIndex(sum(index_lens))
        else:  # concat index returned from partitions
            index_lst = [
                x for part_index in cls.materialize(index_ids) for x in part_index
            ]
            new_index = pandas.Index(index_lst).set_names(index_col)
        new_frame = cls.frame_cls(np.array(partition_ids), new_index, cols_names)
        new_frame.synchronize_labels(axis=0)
        return cls.query_compiler_cls(new_frame)
