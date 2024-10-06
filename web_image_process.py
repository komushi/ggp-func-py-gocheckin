import io
import requests

import PIL.Image
import numpy as np

def read_picture_from_url(url):

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }

    cookies = {
        "cf_clearance": "tBR_Wx9rvie1heZdT9anHH2EbiStozSicOMhhGHJ1LY-1728151494-1.2.1.1-jMocEp7dgPeo.JKnV6eHjVOz5tEmjo74F04QmzCGLsIXTtdM_DripHAD7xqXYc8gSnzmTPFbkf2K56S9BDN7w847Kh_aP5vm28GMamdxiZpiBmE6OPc3tMgoaFAjII56AJ5hjgIA5JYJfhGuRf8CWIuQ4THZMYZM78JgPXFg1ohQ9PU.ZS41NPV6FRn2ZW1GPR66ebmsydk5v082X6VrNjyL6ImhCPL252Gd6j7nGHkH_O5XuqOC01fDd7nxdjthNWCHI5VhW0yrXiwoLZ9y8sF_VjbDtp7HkLACiyc37i.KsXStDEG54ZTxDSVRjmC1cC._DMM_lYcmNW8btHFSk..1btXS6MsvLlvl0d54m6zLC0qlcaItkhtW.8p98buaXaJdbNDqYBiEoFRa.LQodppOYNi88hy02iEzXbOZis8VTUWbu6MIIURuT2Im2tPq; __cf_bm=eSBwPl36.ifinLdccG1emO.HgZeIsKt6uXziCEHvy1I-1728175131-1.0.1.1-CBjYn3a_nCDXNUzPa_44uGOQSUl5CzAROaPwMGgQ05Cns5Gq18BNjWymX42JgxsH1uxqJy4cPbJOPmDmzByI4Q",
    }

    # Download the image
    response = requests.get(url, cookies=cookies, headers=headers)
    response.raise_for_status()  # Ensure the request was successful
    
    # Open the image from the downloaded content    
    image = PIL.Image.open(io.BytesIO(response.content)).convert("RGB")
    
    # Convert the image to a numpy array
    image_array = np.array(image)
    
    # Rearrange the channels from RGB to BGR
    image_bgr = image_array[:, :, [2, 1, 0]]
    
    return image_bgr, image