from app import db, login
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from time import time
import jwt
from flask import current_app
from hashlib import md5
# A 'mixin' class the contains generic implementations that usually
# work as is. Note the Flask-Login ext requires certain properties
# and methods to be implemented.
from flask_login import UserMixin

from app.search import add_to_index, remove_from_index, query_index


class SearchableMixin(object):
    '''
    Custon mixin class to introduce search features to the models below.
    '''
    # Class methods means we can call the function without a class instance.
    # For example, once this mixin is added to Post, we can do Post.search().
    @classmethod
    def search(cls, expression, page, per_page):
        '''
        Replace the list of object IDs with actual objects.
        '''
        # __tablename__ is an SQLAlchemy attribute, which we use as an index.
        ids, total = query_index(cls.__tablename__, expression, page, per_page)
        if total == 0:
            return cls.query.filter_by(id=0), 0
        when = []
        # Collect list of tuples.
        for i in range(len(ids)):
            when.append((ids[i], i))
        # This query uses the list of IDs above to find the objects in the
        # database. The CASE part means we get the results in the order the
        # IDs are given. This is different than results from elasticsearch
        # above, which has results sorted from most to least relevant.
        return cls.query.filter(cls.id.in_(ids)).order_by(
            db.case(when, value=cls.id)), total

    @classmethod
    def before_commit(cls, session):
        '''
        Called when the before_commit event is emitted by SQLAlchemy.
        '''
        # Save these changes in the db session while the session is still
        # open. Otherwise, we don't have access.
        session._changes = {
            'add': list(session.new),
            # Modified items are dirty.
            'update': list(session.dirty),
            'delete': list(session.deleted)
        }

    @classmethod
    def after_commit(cls, session):
        '''
        Called when the after_commit event is emitted by SQLAlchemy.
        Session was committed, so make similar changes to Elasticsearch to keep them in sync.
        '''
        # Use the session changes recorded during before_commit()
        for obj in session._changes['add']:
            if isinstance(obj, SearchableMixin):
                add_to_index(obj.__tablename__, obj)
        for obj in session._changes['update']:
            if isinstance(obj, SearchableMixin):
                add_to_index(obj.__tablename__, obj)
        for obj in session._changes['delete']:
            if isinstance(obj, SearchableMixin):
                remove_from_index(obj.__tablename__, obj)
        session._changes = None

    @classmethod
    def reindex(cls):
        ''' Helper method to refresh data from relational DB to search index.
        '''
        for obj in cls.query:
            add_to_index(cls.__tablename__, obj)


# Bind event handlers to our custom functions.
db.event.listen(db.session, 'before_commit', SearchableMixin.before_commit)
db.event.listen(db.session, 'after_commit', SearchableMixin.after_commit)
'''
Under the Object Relational Manager (ORM) paradigm, relational
databases (i.e. those typically mangaged by SQL) can be managed by classes, objects, and methods instead of tables and SQL.
 translates between high-level operations and database commands.
'''
'''
This is an association table, used to represent many-to-many 
relationships. It uses two foreign keys.
For us, followers has a many-to-many relationship between
users and users, so it's called self-referential.

No model class is made because it doesn't actually hold data, only
foreign keys.
'''
followers = db.Table(
    'followers', db.Column('follower_id', db.Integer,
                           db.ForeignKey('user.id')),
    db.Column('followed_id', db.Integer, db.ForeignKey('user.id')))


# Classes define the structure (or schema) for this app.
# Note the addition of th mixin, adding generic code.
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True)
    email = db.Column(db.String(64), index=True, unique=True)
    password_hash = db.Column(db.String(128))
    about_me = db.Column(db.String(140))
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    # Not an actual field, but defines a relationship.
    # \param: First arg is class of DB model that is the 'many' of this
    # one-to-many relationship.
    # \param: backref - name of field added to 'many' class that points
    # back to this one.
    # \param: lazy - defines how DB will query for the relationship.
    posts = db.relationship('Post', backref='author', lazy='dynamic')

    # Use the association table above to declare the many-many relation
    # See this for all the details: https://blog.miguelgrinberg.com/post/the-flask-mega-tutorial-part-viii-followers
    followed = db.relationship('User',
                               secondary=followers,
                               primaryjoin=(followers.c.follower_id == id),
                               secondaryjoin=(followers.c.followed_id == id),
                               backref=db.backref('followers', lazy='dynamic'),
                               lazy='dynamic')

    # The Werkzeug package comes with Flask and provides some
    # crypto functions, like those used below.
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    # Gravatar provides an easy API to obtain unique avatars based
    # on the hash of an email.
    def avatar(self, size):
        digest = md5(self.email.lower().encode('utf-8')).hexdigest()
        return f'https://www.gravatar.com/avatar/{digest}?d=identicon&s={size}'

    # For followers. Good to put actions here on the model instead of on the view function
    def follow(self, user):
        if not self.is_following(user):
            self.followed.append(user)

    def unfollow(self, user):
        if self.is_following(user):
            self.followed.remove(user)

    def is_following(self, user):
        return self.followed.filter(
            followers.c.followed_id == user.id).count() > 0

    # A single db query to get all the posts of all followed users and sort it
    # by data. With thousands of posts and followed users, this could be
    # expensive to do on the application. So have the db do it.
    # Details: https://blog.miguelgrinberg.com/post/the-flask-mega-tutorial-part-viii-followers
    def followed_posts(self):
        followed = Post.query.join(
            followers, (followers.c.followed_id == Post.user_id)).filter(
                followers.c.follower_id == self.id)
        own = Post.query.filter_by(user_id=self.id)
        return followed.union(own).order_by(Post.timestamp.desc())

    # Use JWT tokens to verify password requets links.
    def get_reset_password_token(self, expires_in=600):
        # arg1 = payload. A dict of the ID of who is resetting password and
        #       and expriraiton time of token
        # arg2 = key to encrypt with
        # arg3 = crytpo algo to use
        # return = a string, more useful than the bytes encode() returns.
        return jwt.encode(
            {
                'reset_password': self.id,
                'exp': time() + expires_in
            },
            current_app.config['SECRET_KEY'],
            algorithm='HS256').decode('utf-8')

    # Verify the link, which includes the JWT, is valid. If so, parse from the
    # payload the user ID.
    # staticmethods can be invoked directly from class, no instance needed
    @staticmethod
    def verify_reset_password_token(token):
        try:
            id = jwt.decode(token,
                            current_app.config['SECRET_KEY'],
                            algorithms=['HS256'])['reset_password']
        except:
            return
        return User.query.get(id)

    # __repr__ tells Python how to print objects of this class
    def __repr__(self):
        return f'<User {self.username}>'


class Post(SearchableMixin, db.Model):
    # This class attribute helps us abstractify full site searching
    # This attr lists the fields that need to be included in the indexing.
    __searchable__ = ['body']

    id = db.Column(db.Integer, primary_key=True)
    body = db.Column(db.String(140))
    # Shows how to use a function to set the value
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    # Shows using a foreign key and how to reference the original table.
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    # To support dynamic translations of posts, record the language
    # when the post is made.
    language = db.Column(db.String(5))

    def __repr__(self):
        return f'<Post {self.body}>'


'''
Flask-Login uses a unique ID to track users and their sessions. To help the extension, a user loader func is required that will get User info from the db.
'''
@login.user_loader
def load_user(id):
    return User.query.get(int(id))