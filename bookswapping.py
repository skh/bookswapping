# main app flow, routes & template
from flask import Flask, render_template, request, redirect, flash, url_for

# login session handling & secret token creation
from flask import session as login_session
import random, string

# OAuth flow
from oauth2client.client import flow_from_clientsecrets
from oauth2client.client import FlowExchangeError

# stuff?
import httplib2
import json
from flask import make_response
import requests

# database logic
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from database import Base, City, Book, User

app = Flask(__name__)
app.debug = True
app.secret_key = "swapyourbooks!"

CLIENT_ID = json.loads(
    open('client_secrets.json', 'r').read())['web']['client_id']

engine = create_engine('postgres://uzpzbcmbkcdqhr:Bi9f0Q7OYDnb9AR3HiHBqwq8_S@ec2-54-204-3-188.compute-1.amazonaws.com:5432/d7q2eacsp9ckel')
Base.metadata.bind = engine

DBSession = sessionmaker(bind=engine)
session = DBSession()

@app.route('/login')
def showLogin():
    state = ''.join(random.choice(string.ascii_uppercase + string.digits) for x in xrange(32))

    login_session['state'] = state
    return render_template('login.html', STATE=state)

@app.route('/gconnect', methods=['POST'])
def gconnect():
    if request.args.get('state') != login_session['state']:
        response = make_response(json.dumps('Invalid state parameter'), 401)
        response.headers['Content-Type'] = 'application/json'
        return response

    code = request.data
    try:
        # Upgrade the authorization code into a credentials object
        oauth_flow = flow_from_clientsecrets('client_secrets.json', scope='')
        oauth_flow.redirect_uri = 'postmessage'
        credentials = oauth_flow.step2_exchange(code)
    except FlowExchangeError:
        response = make_response(json.dumps('Failed to upgrade the authorization code.'), 401)
        response.headers['Content-Type'] = 'application/json'
        return response

    # check that the access token is valid
    access_token = credentials.access_token
    url = 'https://www.googleapis.com/oauth2/v1/tokeninfo?access_token=%s' % access_token
    h = httplib2.Http()
    result = json.loads(h.request(url, 'GET')[1])

    # if there was an error in the access token info, abort
    if result.get('error') is not None:
        response = make_response(json.dumps(result.get('error')), 500)
        response.headers['Content-Type'] = 'application/json'
        return response

    # verify that the access token is used for the intended user
    gplus_id = credentials.id_token['sub']
    if result['user_id'] != gplus_id:
        response = make_response(
            json.dumps('Token\'s user ID does not match given user ID.'), 401)
        response.headers['Content-Type'] = 'application/json'
        return response

    # verify that the access token is valid for this app
    if result['issued_to'] != CLIENT_ID:
        response = make_response(
            json.dumps('Token\'s client ID does not match the application\'s.'))
        response.headers['Content-Type'] = 'application/json'
        return response

    # check if the user is already logged in
    stored_credentials = login_session.get('credentials')
    stored_gplus_id = login_session.get('gplus_id')
    if stored_credentials is not None and gplus_id == stored_gplus_id:
        response = make_response(
            json.dumps('Current user is already connected.'), 200)
        response.headers['Content-Type'] = 'application/json'
        return response

    # store the access token in the session for later use
    login_session['credentials'] = credentials.to_json()
    login_session['gplus_id'] = gplus_id

    # get user info
    userinfo_url = "https://www.googleapis.com/oauth2/v1/userinfo"
    params = {'access_token': credentials.access_token,
              'alt': 'json'}
    answer = requests.get(userinfo_url, params=params)
    data = json.loads(answer.text)

    login_session['username'] = data['name']
    login_session['picture'] = data['picture']
    login_session['email']  = data['email']

    user_id = getUserID(login_session['email'])
    if not user_id:
        user_id = createUser(login_session)
        
    login_session['user_id'] = user_id

    flash('You are now logged in as %s' % login_session['username'])

    return render_template(
        'welcome.html', 
        username=login_session['username'],
        picture=login_session['picture'],
        email=login_session['email'])

# disconnect -- Revoke a current user's token and reset their login_session
@app.route('/gdisconnect')
def gdisconnect():
    # only disconnect a connected user
    credentials = json.loads(login_session.get('credentials'))
    if credentials is None:
        response = make_response(
            json.dumps('Current user not connected.'), 401)
        response.headers['Content-Type'] = 'application/json'
        return response
    # execute  HTTP GET request to revoke current token
    access_token = credentials.get('access_token')
    url = 'https://accounts.google.com/o/oauth2/revoke?token=%s' % access_token
    h = httplib2.Http()
    result = h.request(url, 'GET')[0]
    # always reset the user's session
    del login_session['credentials']
    del login_session['gplus_id']
    del login_session['username']
    del login_session['email']
    del login_session['picture']

    if result['status'] == '200':
        response = make_response(
            json.dumps('Successfully disconnected.'), 200)
        response.headers['Content-Type'] = 'application/json'
        return response
    else:
        # for whatever reason, the given token was invalid
        response = make_response(
            json.dumps('Failed to revoke token for given user.'), 400)
        response.headers['Content-Type'] = 'application/json'
        return response

@app.route('/')
@app.route('/cities')
def cityList():
    cities = session.query(City, func.count(Book.id)).outerjoin(Book).group_by(City).all()
    return render_template('cities.html', cities = cities)

@app.route('/cities/new', methods=['GET','POST'])
def newCity():
    if 'username' not in login_session:
        return redirect('/login') 
    if request.method == 'POST':
        if request.form['token'] != login_session['token']:
            # no flash message, we don't answer CSRFs
            return redirect('/cities')
        newCity = City(name = request.form['name'])
        session.add(newCity)
        session.commit()
        flash("New city %s was successfully added." % request.form['name'])
        return redirect(url_for('cityList'))
    else:
        # token to protect against CSRF (cross-site request forgery)
        login_session['token'] = ''.join(
            random.choice(string.ascii_uppercase + string.digits) for x in xrange(16))
        return render_template('newcity.html', TOKEN=login_session['token'])

@app.route('/cities/<int:city_id>/edit', methods=['GET','POST'])
def editCity(city_id):
    # not logged in
    if 'username' not in login_session:
        flash("You need to be logged in to edit a city.")
        return redirect('/login')

    city_to_edit = session.query(City).filter_by(id=city_id).one()

    if not city_to_edit:
        flash("There is no city with id %d" % city_id)
        return redirect('/cities')

    if request.method == 'POST':
        if request.form['token'] != login_session['token']:
            # no flash message, we don't answer CSRFs
            return redirect('/cities')

        if request.form['name']:
            city_to_edit.name = request.form['name']
            message = "The city %s was successfully edited." % city_to_edit.name
            session.add(city_to_edit)
            session.commit()
            flash(message)

        return redirect('/cities')

    else:
        # token to protect against CSRF (cross-site request forgery)
        login_session['token'] = ''.join(
            random.choice(string.ascii_uppercase + string.digits) for x in xrange(16))
        return render_template('editcity.html', city=city_to_edit, TOKEN=login_session['token'])


@app.route('/cities/<int:city_id>/delete', methods=['GET','POST'])
def deleteCity(city_id):
    # not logged in
    if 'username' not in login_session:
        flash("You need to be logged in to delete a city.")
        return redirect('/login')

    city_to_delete = session.query(City).filter_by(id=city_id).one()
    
    if not city_to_delete:
        flash("There is no city with id %d" % city_id)
        return redirect('/cities')
    if request.method == 'POST':
        if request.form['token'] != login_session['token']:
            # no flash message, we don't answer CSRFs
            return redirect('/cities')
        message = "The city %s was successfully deleted." % city_to_delete.name
        session.delete(city_to_delete)
        session.commit()
        flash(message)
        return redirect('/cities')

    else:
        # token to protect against CSRF (cross-site request forgery)
        login_session['token'] = ''.join(
            random.choice(string.ascii_uppercase + string.digits) for x in xrange(16))
        # if a city still contains books, we will not delete it, but show a message
        # and a list of the books -- see template
        books = session.query(Book).filter_by(city_id=city_to_delete.id).all()
        return render_template('deletecity.html', city=city_to_delete, books=books, TOKEN=login_session['token'])

@app.route('/cities/<int:city_id>/books')
def bookList(city_id):
    books = session.query(Book).filter_by(city_id=city_id).all()
    city = session.query(City).filter_by(id=city_id).one()
    print books
    return render_template('books.html', books=books, city=city)

@app.route('/cities/<int:city_id>/books/new', methods=['GET','POST'])
def newBook(city_id):
    if 'username' not in login_session:
        return redirect('/login')

    city = session.query(City).filter_by(id=city_id).one()

    if not city:
        flash("There is no city with id %d" % city_id)
        return redirect('/cities')

    if request.method == 'POST':
        if request.form['token'] != login_session['token']:
            # no flash message, we don't answer CSRFs
            return redirect('/cities')
        newBook = Book(title=request.form['title'], author=request.form['author'], city_id=city_id)
        session.add(newBook)
        session.commit()

        flash("New book was successfully added: %s" % request.form['title'])
        return redirect(url_for('bookList', city_id=city_id))
    else:
        # token to protect against CSRF (cross-site request forgery)
        login_session['token'] = ''.join(
            random.choice(string.ascii_uppercase + string.digits) for x in xrange(16))
        return render_template('newbook.html', city=city, TOKEN=login_session['token'])

@app.route('/cities/<int:city_id>/books/<int:book_id>/edit', methods=['GET','POST'])
def editBook(city_id, book_id):
    # not logged in
    if 'username' not in login_session:
        flash("You need to be logged in to edit a book.")
        return redirect('/login')

    book_to_edit = session.query(Book).filter_by(id=book_id).one()
    city = session.query(City).filter_by(id=city_id).one()

    if not book_to_edit:
        flash("There is no book with id %d" % book_id)
        return redirect('/cities')

    if not city:
        flash("There is no city with id %d" % city_id)
        return redirect('/cities')

    if request.method == 'POST':
        if request.form['token'] != login_session['token']:
            # no flash message, we don't answer CSRFs
            return redirect('/cities')

        if request.form['title'] and request.form['author']:
            book_to_edit.title = request.form['title']
            book_to_edit.author = request.form['author']
            message = "The book %s by %s was successfully edited." % (
                book_to_edit.title, book_to_edit.author)
            session.add(book_to_edit)
            session.commit()
            flash(message)

        return redirect('/cities')

    else:
        # token to protect against CSRF (cross-site request forgery)
        login_session['token'] = ''.join(
            random.choice(string.ascii_uppercase + string.digits) for x in xrange(16))
        return render_template('editbook.html', book=book_to_edit, 
            city=city, TOKEN=login_session['token'])


@app.route('/cities/<int:city_id>/books/<int:book_id>/delete', methods=['GET','POST'])
def deleteBook(city_id, book_id):
    # not logged in
    if 'username' not in login_session:
        flash("You need to be logged in to delete a book.")
        return redirect('/login')

    book_to_delete = session.query(Book).filter_by(id=book_id, city_id=city_id).one()
    city = session.query(City).filter_by(id=city_id).one()
    
    if not city:
        flash("There is no city with id %d" % city_id)
        return redirect('/cities')
    if not book_to_delete:
        flash("There is no book with id %d" % book_id)
    if request.method == 'POST':
        if request.form['token'] != login_session['token']:
            # no flash message, we don't answer CSRFs
            return redirect('/cities')
        message = "The book %s by %s was successfully deleted." % (
            book_to_delete.title, book_to_delete.author)
        session.delete(book_to_delete)
        session.commit()
        flash(message)
        return redirect(url_for('bookList', city_id=city_id))

    else:
        # token to protect against CSRF (cross-site request forgery)
        login_session['token'] = ''.join(
            random.choice(string.ascii_uppercase + string.digits) for x in xrange(16))
        return render_template('deletebook.html', city=city, book=book_to_delete, TOKEN=login_session['token'])

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')

def getUserID(email):
    try:
        user = session.query(User).filter_by(email=email).one()
        return user.id
    except:
        return None

def getUserInfo(user_id):
    user = session.query(User).filter_by(id=user_id).one()
    return user

def createUser(login_session):
    newUser = User(name=login_session['username'],
                    email=login_session['email'],
                    picture=login_session['picture'])
    session.add(newUser)
    session.commit()
    user = session.query(User).filter_by(email=login_session['email']).one()
    return user.id


if __name__ == '__main__':
    app.secret_key = "swapyourbooks!"
    app.debug = True
    app.run(host = '0.0.0.0', port = 5000)
