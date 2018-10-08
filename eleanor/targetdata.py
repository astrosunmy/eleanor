import numpy as np
import matplotlib.pyplot as plt
from astropy.nddata import Cutout2D
from photutils import CircularAperture, RectangularAperture, aperture_photometry
from lightkurve import KeplerTargetPixelFile as ktpf
from lightkurve import SFFCorrector
from scipy import ndimage
from scipy.optimize import minimize

from ffi import use_pointing_model
from postcard import Postcard

__all__ = ['TargetData']

class TargetData(object):
    """
    Object containing the light curve, target pixel file, and related information
    for any given source.

    Parameters
    ----------
    source : an ellie.Source object

    Attributes
    ----------
    tpf : [lightkurve TargetPixelFile object](https://lightkurve.keplerscience.org/api/targetpixelfile.html)
        target pixel file
    best_lightcurve : [lightkurve LightCurve object](https://lightkurve.keplerscience.org/api/lightcurve.html)
        extracted light curve
    centroid_trace : (2, N_time) np.ndarray
        (xs, ys) in TPF pixel coordinates as a function of time
    best_aperture :

    all_lightcurves
    all_apertures
    """
    def __init__(self, source):
        self.post_obj = Postcard(source.postcard)
        self.time = self.post_obj.time
        self.load_pointing_model(source.sector, source.camera, source.chip)
        self.get_tpf_from_postcard(source.coords, source.postcard)
        self.create_apertures()
        self.get_lightcurve()


    def load_pointing_model(self, sector, camera, chip):
        from astropy.table import Table
        import urllib
        pointing_link = urllib.request.urlopen('http://jet.uchicago.edu/tess_postcards/pointingModel_{}_{}-{}.txt'.format(sector,
                                                                                                                          camera,
                                                                                                                          chip))
        pointing = pointing_link.read().decode('utf-8')
        pointing = Table.read(pointing, format='ascii.basic') # guide to postcard locations  
        self.pointing_model = pointing
        return
                                                                                                                          
        
    def get_tpf_from_postcard(self, pos, postcard):
        """
        Creates a FITS file for a given source that includes:
            Extension[0] = header
            Extension[1] = (9x9xn) TPF, where n is the number of cadences in an observing run
            Extension[2] = (3 x n) time, raw flux, systematics corrected flux
        Defines
            self.tpf     = flux cutout from postcard
            self.tpf_err = flux error cutout from postcard
            self.centroid_xs = pointing model corrected x pixel positions
            self.centroid_ys = pointing model corrected y pixel positions
        """
        from astropy.wcs import WCS
        from muchbettermoments import quadratic_2d
        from astropy.nddata import Cutout2D

        self.tpf = None
        self.centroid_xs = None
        self.centroid_ys = None

        def apply_pointing_model(xy):
            centroid_xs, centroid_ys = [], []
            for i in range(len(self.pointing_model)):
                new_coords = use_pointing_model(xy, self.pointing_model[i])
                centroid_xs.append(new_coords[0][0])
                centroid_ys.append(new_coords[0][1])
            self.centroid_xs = centroid_xs
            self.centroid_ys = centroid_ys
            return

        xy = WCS(self.post_obj.header).all_world2pix(pos[0], pos[1], 1)
        apply_pointing_model(xy)

        # Define tpf as region of postcard around target
        med_x, med_y = np.nanmedian(self.centroid_xs), np.nanmedian(self.centroid_ys)
        med_x, med_y = int(np.floor(med_x)), int(np.floor(med_y))

        post_flux = np.transpose(self.post_obj.flux, (2,0,1))        
        post_err  = np.transpose(self.post_obj.flux_err, (2,0,1))
        
        self.tpf  = post_flux[:, med_y-4:med_y+5, med_x-4:med_x+5]
        self.tpf_err = post_err[:, med_y-4:med_y+5, med_x-4:med_x+5]
        return


    def create_apertures(self):
        """
        Finds the "best" aperture (i.e. the one that produces the smallest std light curve) for a range of
        sizes and shapes.
        Defines
            self.all_lc = an array of light curves from all apertures tested
            self.all_aperture = an array of masks for all apertures tested
        """
        from photutils import CircularAperture, RectangularAperture

        self.all_lc       = None
        self.all_aperture = None

        # Creates a circular and rectangular aperture
        def circle(pos,r):
            return CircularAperture(pos,r)
        def rectangle(pos, l, w, t):
            return RectangularAperture(pos, l, w, t)

        # Completes either binary or weighted aperture photometry
        def binary(data, apertures, err):
            return aperture_photometry(data, apertures, error=err, method='center')
        def weighted(data, apertures, err):
            return aperture_photometry(data, apertures, error=err, method='exact')

        r_list = np.arange(1.5,4,0.5)

        # Center gives binary mask; exact gives weighted mask
        circles, rectangles, self.all_apertures = [], [], []
        for r in r_list:
            ap_circ = circle( (4,4), r )
            ap_rect = rectangle( (4,4), r, r, 0.0)
            circles.append(ap_circ); rectangles.append(ap_rect)
            for method in ['center', 'exact']:
                circ_mask = ap_circ.to_mask(method=method)[0].to_image(shape=((
                            np.shape( self.tpf[0]))))
                rect_mask = ap_rect.to_mask(method=method)[0].to_image(shape=((
                            np.shape( self.tpf[0]))))
                self.all_apertures.append(circ_mask)
                self.all_apertures.append(rect_mask)
        return



    def get_lightcurve(self, custom_mask=False):
        """   
        Extracts a light curve using the given aperture and TPF.
        Allows the user to pass in a mask to use, otherwise sets
            "best" lightcurve and aperture (min std)    
        Mask is a 2D array of the same shape as TPF (9x9)
        Defines:     
            self.lc  
            self.lc_err
            self.aperture
            self.all_lc     (if mask=None)
            self.all_lc_err (if mask=None
        """

        self.flux       = None
        self.flux_err   = None
        self.aperture   = None

        if custom_mask is False:

            self.all_lc     = None
            self.all_lc_err = None

            all_lc     = np.zeros((len(self.all_apertures), len(self.tpf)))
            all_lc_err = np.zeros((len(self.all_apertures), len(self.tpf)))

            stds = []
            for a in range(len(self.all_apertures)):
                for cad in range(len(self.tpf)):
                    all_lc_err[a][cad] = np.sqrt( np.sum( self.tpf_err[cad]**2 * self.all_apertures[a] ))
                    all_lc[a][cad]     = np.sum( self.tpf[cad] * self.all_apertures[a] )
                stds.append( np.std(all_lc[a]) )
            self.all_lc     = np.array(all_lc)
            self.all_lc_err = np.array(all_lc_err)
            
            best_ind = np.where(stds == np.min(stds))[0][0]
            self.lc       = self.all_lc[best_ind]
            self.aperture = self.all_apertures[best_ind]
            self.lc_err   = self.all_lc_err[best_ind]

        else:
            if self.custom_aperture is None:
                print("You have not defined a custom aperture. You can do this by calling the function .custom_aperture")
            else:
                lc = np.zeros(len(self.tpf))
                for cad in range(len(self.tpf)):
                    lc[cad]     = np.sum( self.tpf[cad]     * self.custom_aperture )
                    lc_err[cad] = np.sum( self.tpf_err[cad] * self.custom_aperture )
                self.flux       = lc
                self.flux_err   = lc_err
                self.aperture   = mask

        return



    def custom_aperture(self, shape=None, r=0.0, l=0.0, w=0.0, theta=0.0, pos=(4,4), method='exact'):
        """
        Allows the user to input their own aperture of a given shape (either 'circle' or
            'rectangle' are accepted) of a given size {radius of circle: r, length of rectangle: l,
            width of rectangle: w, rotation of rectangle: t}
        Pos is the position given in pixel space
        Method defaults to 'exact'
        Defines
            self.custom_aperture: 2D array of shape (9x9)
        """
        from photutils import CircularAperture, RectangularAperture

        self.custom_aperture = None

        shape = shape.lower()

        if shape == 'circle':
            if r == 0.0:
                print ("Please set a radius (in pixels) for your aperture")
            else:
                aperture = CircularAperture(pos=pos, r=r)
                self.custom_aperture = aperture.to_mask(method=method)[0].to_image(shape=((
                            np.shape(self.tpf[0]))))

        elif shape == 'rectangle':
            if l==0.0 or w==0.0:
                print("For a rectangular aperture, please set both length and width: custom_aperture(shape='rectangle', l=#, w=#)")
            else:
                aperture = RectangularAperture(pos=pos, l=l, w=w, t=theta)
                self.custom_aperture = aperture.to_mask(method=method)[0].to_image(shape=((
                            np.shape(self.tpf[0]))))
        else:
            print("Aperture shape not recognized. Please set shape == 'circle' or 'rectangle'")
        return



    def jitter_corr(self):
        """
        Corrects for jitter in the light curve by quadratically regressing with centroid position.
        """
        x_pos, y_pos = self.centroid_x, self.centroid_y
        lc = self.lc

        def parabola(params, x, y, f_obs, y_err):
            c1, c2, c3, c4, c5 = params
            f_corr = f_obs * (c1 + c2*(x-2.5) + c3*(x-2.5)**2 + c4*(y-2.5) + c5*(y-2.5)**2)
            return np.sum( ((1-f_corr)/y_err)**2)

        # Masks out anything >= 2.5 sigma above the mean
        mask = np.ones(len(lc), dtype=bool)
        for i in range(5):
            lc_new = []
            std_mask = np.std(lc[mask])

            inds = np.where(lc <= np.mean(lc)-2.5*std_mask)
            y_err = np.ones(len(lc))**np.std(lc)
            for j in inds:
                y_err[j] = np.inf
                mask[j]  = False

            if i == 0:
                initGuess = [3, 3, 3, 3, 3]
            else:
                initGuess = test.x

            bnds = ((-15.,15.), (-15.,15.), (-15.,15.), (-15.,15.), (-15.,15.))
            test = minimize(parabola, initGuess, args=(x_pos, y_pos, lc, y_err), bounds=bnds)
            c1, c2, c3, c4, c5 = test.x
            lc_new = lc * (c1 + c2*(x_pos-2.5) + c3*(x_pos-2.5)**2 + c4*(y_pos-2.5) + c5*(y_pos-2.5)**2)

        self.lc = np.copy(lc_new)


    def rotation_corr(self):
        """ Corrects for spacecraft roll using Lightkurve """
        time = np.arange(0, len(self.lc), 1)
        sff = SFFCorrector()
        x_pos, y_pos = self.centroid_x, self.centroid_y
        lc_new = sff.correct(time, self.lc, x_pos, y_pos, niters=1,
                                   windows=1, polyorder=5)
        self.lc = np.copy(lc_new)


    def system_corr(self, jitter=False, roll=False):
        """
        Allows for systematics correction of a given light curve
        """
        if jitter==True:
            self.jitter_corr()
        if roll==True:
            self.rotation_corr()
