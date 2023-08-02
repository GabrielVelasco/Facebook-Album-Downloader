# facebook already has an option to donwload the entire album but you must be the owner of the album
# and sometimes it fails to download albums with to many pictures
# using this script you'll be able download the album without being the owner of it

from selenium import webdriver
from bs4 import BeautifulSoup
import time
import requests
import shutil # to save the img locally
import os

def removeInvalidCharacters(ALBUMS_TITLE):
    ALBUMS_TITLE = ALBUMS_TITLE.replace('/', '.') # replace invalid characters
    ALBUMS_TITLE = ALBUMS_TITLE.replace(':', '-') # replace invalid characters

    return ALBUMS_TITLE

def createFolder(MAIN_FOLDER, ALBUMS_TITLE):
    # main folders name will be like 'downloadedImgs'
    # creates the albums folder inside mainFolder

    # if folder 'mainFolder' doesn't exist, create one
    if not os.path.exists(MAIN_FOLDER):
        os.mkdir(MAIN_FOLDER)

    fullPath = f'{MAIN_FOLDER}\{ALBUMS_TITLE}'    
    if not os.path.exists(fullPath):
        os.mkdir(fullPath)

def requestOk(code):
    return code == 200

def downloadImgs(IMG_SRC_LIST, MAIN_FOLDER, ALBUMS_FOLDER):
    # download all pictures 
    # from a given list of urls (album)
    # save to 'mainFolder/albumsFolder'

    # download an img at each iteration
    for iteration, imgSrc in enumerate(IMG_SRC_LIST):
        res = requests.get(imgSrc, stream=True)

        fileName = str(iteration + 1) + ".jpg"   # X.jpg
        fullPath = f'{MAIN_FOLDER}\{ALBUMS_FOLDER}\{fileName}'

        if requestOk(res.status_code):
            with open(fullPath, 'wb') as f:
                shutil.copyfileobj(res.raw, f)

        else:
            print('Image Couldn\'t be retrieved')

def loadsAllAlbumsPictures(driver):
    # scroll down to the bottom of the albums page
    # so it loads all pictures

    # scripts that runs at the browser's console
    scriptToGetScrollHeight = "return document.body.scrollHeight" # ammount of scrolling space
    scriptToScrollDown = "window.scrollTo(0, document.body.scrollHeight);"

    # Script to scroll down to the bottom of the page so it loads all <a>
    SCROLL_PAUSE_TIME = 1

    last_height = driver.execute_script(scriptToGetScrollHeight)

    while True:
        driver.execute_script(scriptToScrollDown)

        time.sleep(SCROLL_PAUSE_TIME)

        # Calculate new scroll height and compare with last scroll height
        new_height = driver.execute_script(scriptToGetScrollHeight)
        if new_height == last_height:
            break # no more scrolling

        # there's still space to scroll down
        last_height = new_height

def buildPhotoUrl(anchorTag):
    BASE_URL = "https://www.facebook.com"

    hrefAttr = anchorTag.attrs['href']
    photoUrl = BASE_URL + hrefAttr

    return photoUrl

def buildImgsUrlList(driver, ANCHOR_TAGS_LIST):
    # build a list of imgs urls
    # from each <a>, get the 'href'

    IMG_TAG_CLASS_NAME = "x85a59c x193iq5w x4fas0m x19kjcj4"
    IMG_SRC_LIST = []

    for anchorTag in ANCHOR_TAGS_LIST:

        photoUrl = buildPhotoUrl(anchorTag)
        
        # drive to photo url
        driver.get(photoUrl)
        time.sleep(1)
        
        # scrap to extract the <img> src attr
        htmlContent = driver.page_source
        soupHtmlTree = BeautifulSoup(htmlContent, features="html.parser")

        imgTag = soupHtmlTree.find('img', attrs={'class': IMG_TAG_CLASS_NAME})
        imgSrc = imgTag.attrs['src']

        IMG_SRC_LIST.append(imgSrc)

    return IMG_SRC_LIST

def doScraping():
    ALBUM_URL = "https://www.facebook.com/some_albums_url"
    A_TAG_CLASS_NAME = "x1i10hfl xjbqb8w x6umtig x1b1mbwd xaqea5y xav7gou x9f619 x1ypdohk xt0psk2 xe8uvvx xdj266r x11i5rnm xat24cr x1mh8g0r xexx8yu x4uap5 x18d9i69 xkhd6sd x16tdsg8 x1hl2dhg xggy1nq x1a2a7pz x1heor9g xt0b8zv"
    ALBUMS_TITLE_CLASS_NAME = "xwoyzhm x1rhet7l"

    driver = webdriver.Firefox()
    driver.get(ALBUM_URL)
    time.sleep(2.5)

    loadsAllAlbumsPictures(driver)

    # there is no more scroll down, page is completely loaded
    htmlContent = driver.page_source
    soupHtmlTree = BeautifulSoup(htmlContent, features="html.parser") # returns an obj, html tree
  
    # Scrap for each pictures's <a> and albums title
    ANCHOR_TAGS_LIST = soupHtmlTree.findAll('a', attrs={'class': A_TAG_CLASS_NAME}) # build list of <a>
    ALBUMS_TITLE = soupHtmlTree.find('div', attrs={'class': ALBUMS_TITLE_CLASS_NAME}).text
    print(f'there is {len(ANCHOR_TAGS_LIST)} photos at this album')

    IMG_SRC_LIST = buildImgsUrlList(driver, ANCHOR_TAGS_LIST)
    
    # IMG_SRC_LIST is completely populated
    MAIN_FOLDER = "downloadedImgs"
    ALBUMS_TITLE = removeInvalidCharacters(ALBUMS_TITLE)
    createFolder(MAIN_FOLDER, ALBUMS_TITLE) # create the albums folder before downloading the album
    downloadImgs(IMG_SRC_LIST, MAIN_FOLDER, ALBUMS_TITLE)

    driver.close()

if __name__ == "__main__": # runs only if this is the main program executed
    doScraping()
