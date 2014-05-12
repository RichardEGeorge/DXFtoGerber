#!/usr/bin/env python

# Convert a DXF file to a series of Gerber and Excellon files suitable for e.g. PCB Train
#
# Layer names on the DXF file are used to determine how the objects in the DXF file map onto the circuit board
# 
# A two-layer PCB can be represented as follows:
#
# (G) Top Overlay           -> .gto
# (G) Top Soldermask        -> .gts
# (G) Top Copper            -> .gtl
# (E) Drill                 -> .gdd
# (G) Bottom Copper         -> .gbl
# (G) Bottom Overlay        -> .gbo
# (G) Bottom Soldermask     -> .gbs
# (E) Mechanical            -> .gm1
#
# where 'E' will become an Excellon file (CNC drill or routing), and 'G' will become a Gerber file (PCB exposure)
#
# The converter knows how to handle two types of objects, polylines and circles
#
# For Gerber files
# 
# Closed polylines -> translate into filled areas of copper, open solder masks, and filled overlay
# Open polylines -> tracks on the copper layer, lines on the overlay/silkscreen
# Circles -> circular aperture flashes on copper, silkscreen and soldermask layers
#
# For Excellon files
# 
# Circles -> drilled holes on 'Drill' layer
# Open Polylines -> Cut-out slots on Mechanical layer
# Closed Polylines -> Cut-out pieces, or boundary of whole PCB
#
# Open polylines need the "Global Linewidth" property set in AutoCAD to define how wide 
#
# Deficiencies / to be implemented:
# 
# Could process drills sensibly: we currently output tool codes for unused holes
# Cut-outs are not implemented on the mechanical layer
#


import math;
import re;
import os;
import glob;

class DXFFile:
    
    X = 10;
    Y = 20;
    Z = 30;
    DIAMETER = 40;
    LINEWIDTH = 41;
    POLYLINE_FLAGS = 70;
    BULGE = 42;
    LAYER = 8;
        
    POLYLINE_FLAG_CLOSED = 1;
        
    prec = 8;
    
    def __init__(self,fname):
        self.polylines = list();
        self.circles = list();
        self.layers = set();
        self.filename = fname;
        
		# An entry in a DXF file consists of
		# integer
		# text
		#
		# e.g.
		# 10
		# x-coordinate
		# 20
		# y-coordinate
		#
		# We build a dictionary that allows look-up of a parser for the next line of input, based on the integer code
		#
        rev_parse = { (lambda a: round(float(a)*self.prec)/self.prec):(self.X,self.Y,self.Z,self.LINEWIDTH,self.BULGE,self.DIAMETER), (lambda a: str(a)):(self.LAYER,), (lambda a: int(a)):(self.POLYLINE_FLAGS,) };
        self.parse = dict();
        for k in rev_parse:
            for j in rev_parse[k]:
                self.parse[j]=k;    
        
        with open(fname) as f:
            self.read_dxf_file(f);
    
    def read_entity(self,f):
        results = {};
        
        while True:
            try:
                t = f.readline().strip();
                l1=int(t);
            except ValueError:
                try:
                    l1=int(t,16);
                except ValueError:
                    l1 = f.readline();
                    continue;
            
            if l1==0:
                break;
            
            if l1 in self.parse:
                l2=f.readline().strip();
                results[l1]=self.parse[l1](l2);
            
        return results;
        
    def read_circle(self,f):
        c = self.read_entity(f);
		# return diameter, not radius
        if 40 in c:
            c[40] = c[40] * 2.0; 
        self.circles.append(c);
        
    def read_polyline(self,f):
        result = list();
        this_entity = self.read_entity(f);
        
        while True:
            l = f.readline().strip();

            if l=='SEQEND':
                break;
 
            if l=='VERTEX':
                result.append(self.read_entity(f));

        this_entity['VERTICES'] = result;
        
        self.polylines.append(this_entity);
            
    def read_dxf_file(self,f):
        while True:
            l=f.readline().strip();
            if l=='EOF':
                break;
            
            if l=='CIRCLE':
                self.read_circle(f);
                
            if l=='POLYLINE':
                self.read_polyline(f);
  
    @staticmethod          
    def matches(a,b):
        return re.sub(' ','_',a.strip().lower())==re.sub(' ','_',b.strip().lower());
        
    def circles_on_layer(self,n):
        for c in self.circles:
            if self.matches(c[self.LAYER],n):
                yield c;
                
    def polylines_on_layer(self,n):
        for p in self.polylines:
            if self.matches(p[self.LAYER],n):
                yield p;
                
    def open_polylines_on_layer(self,n):
        for p in self.polylines:
            if self.matches(p[self.LAYER],n):
                if self.POLYLINE_FLAGS not in p:
                    yield p;
                else:
                    if (p[self.POLYLINE_FLAGS] & self.POLYLINE_FLAG_CLOSED)==0:
                        yield p;
    
    def closed_polylines_on_layer(self,n):
        for p in self.polylines:
            if self.matches(p[self.LAYER],n):
                if self.POLYLINE_FLAGS in p:
                    if (p[self.POLYLINE_FLAGS] & self.POLYLINE_FLAG_CLOSED)!=0:
                        yield p;
      
    def diameters(self,circles,layer='ALL'):
        result = set();
        for c in circles:
			if (self.matches(c[self.LAYER],layer)) | (layer=='ALL'):
				result.add(c[self.DIAMETER]);
        return result;
        
    def linewidths(self,polylines,layer='ALL'):
        result = set();
        for p in polylines:
			if (self.matches(p[self.LAYER],layer)) | layer=='ALL':
				result.add(p[self.LINEWIDTH]);
        return result;
        
    def layer_names(self):
        result = set();
        for c in self.circles:
            result.add(c[self.LAYER]);
        for p in self.polylines:
            result.add(p[self.LAYER]);
        return result;
                
class GerberWriter:
    
    precision = (2,6);
    scale = 1.0;
    default_diameter = 0.01;
    
    gerber_layers = { \
        '.gbl':('Bottom Copper','Bottom'), \
        '.gbo':('Bottom Outlines','Bottom Overlay'), \
        '.gbs':('Bottom Soldermask',), \
        '.gtl':('Top Copper','Top',), \
        '.gto':('Top Outlines','Top Overlay'), \
        '.gts':('Top Soldermask',)};
        
    mechanical_layers = {'.gm1':('Mechanical','Cutout','Cut Out')};
            
    excellon_layers = { \
        '.gdd':('Drill',) };
            
    @staticmethod
    def emit_coord(d):
        s = d * GerberWriter.scale;
        result = ('%%%dd' % (GerberWriter.precision[0]+GerberWriter.precision[1])) % int(s*pow(10.0,GerberWriter.precision[1]));
        return result.strip();

    @staticmethod
    def exc_emit_coord(d):
        s = d * GerberWriter.scale;
        return re.sub('^0+','','%06.2f' % (s));
    
    @staticmethod
    def exc_emit_point(p):
        result ='X%s' % GerberWriter.exc_emit_coord(p[0]);
        result +='Y%s' % GerberWriter.exc_emit_coord(p[1]);
        return result;
        
    def flash_command(self,f,p,c):
        self.emit_command(f,self.emit_point(p)+c);
        
    @staticmethod
    def emit_command(f,symbol,value=""):
        if symbol=='G04':
            print >> f, "G04 %s*" % (value.strip());
        else:
            print >> f, "%s%s*" % (symbol,value);
            
    @staticmethod
    def emit_parameter(f,p,value):
        print >> f,"%%%s%s*%%" % (p,value);
    
    @staticmethod
    def emit_precision(f):
        GerberWriter.emit_parameter(f,"FS","LAX%d%d" % (GerberWriter.precision[0],GerberWriter.precision[1]));
         
    @staticmethod
    def XthenY(a,b):
        if a[DXFFile.X] < b[DXFFile.X]:
            return -1;
        else:
            if a[DXFFile.X] > b[DXFFile.X]:
                return 1;
            else:
                if a[DXFFile.Y] < b[DXFFile.Y]:
                    return -1;
                else:
                    if a[DXFFile.Y] > b[DXFFile.Y]:
                        return 1;
                    else:
                        return a[DXFFile.DIAMETER] < b[DXFFile.DIAMETER]; 
                        
    @staticmethod
    def no_duplicates(k):
        First = True;
        for j in k:
            if First:
                i = j;
                yield i;
                First = False;
            else:
                if GerberWriter.XthenY(i,j)==0:
                    continue;
                else:
                    i = j;
                    yield i;  
        
    def emit_point(self,p):
        result='';
        if self.X!=p[0]:
            result +='X%s' % GerberWriter.emit_coord(p[0]);
            self.X=p[0];
            
        if self.Y!=p[1]:
            result +='Y%s' % GerberWriter.emit_coord(p[1]);
            self.Y=p[1];
            
        return result;
        
    def draw_to(self,f,p):
        self.emit_command(f,self.emit_point(p)+"D01");

    def move_to(self,f,p):
        self.emit_command(f,self.emit_point(p)+"D02");
        
    def emit_level(self,f,dark=True):
        if dark:
            self.emit_parameter(f,"LP","D");
            self.level_dark = True;
        else:
            self.emit_parameter(f,"LP","C");
            self.level_dark = False;
        
    def clear_aperture_cache(self):
        self.circular_apertures = set();
        self.aperture_codes = dict();
        self.aperture_diameters = dict();
        
        self.excellon_drill_diameters = dict();
        self.excellon_drill_codes = dict();

        self.current_aperture_code = -1;
        
        self.excellon_drill_counter = -1;    
        self.current_excellon_drill_code = -1;
        
        
    ## INIT METHOD

    def __init__(self):
        self.clear_aperture_cache();
                
    '''Measure the DXF file, record circular apertures'''
    def measure_dxf_file(self,dxf):

        for c in dxf.circles:
            self.circular_apertures.add(c[DXFFile.DIAMETER]);
            
        for p in dxf.polylines:
            if DXFFile.LINEWIDTH in p:
                self.circular_apertures.add(p[DXFFile.LINEWIDTH]);                    
            else:
                self.circular_apertures.add(0.0);
                                        
    def process_dxf_for_writing(self,dxf,layernames):
        
        regions = list();
        tracks = list();
        circles = list();
        
        # Process tracks
        for layer in layernames:
            for p in dxf.open_polylines_on_layer(layer):
                tracks.append(p);
                
        # Process regions
        for layer in layernames:
            for p in dxf.closed_polylines_on_layer(layer):
                regions.append(p);

        # Process circles        
        for layer in layernames:
            for p in dxf.circles_on_layer(layer):
                circles.append(p);
                
        return {'Tracks':tracks,'Regions':regions,'Circles':circles};
        
    def emit_gerber_aperture_definition(self,f,n,s):
        self.emit_parameter(f,"ADD%d" % n,s);
            
    def define_gerber_circular_aperture(self,f,n,d):
        
        dia = self.default_diameter if d==0.0 else d;
                    
        self.emit_gerber_aperture_definition(f,n,"C,%f" % (dia*self.scale));
        
        self.aperture_diameters[d]=n;
        self.aperture_codes[n]=d;
        
    def ensure_region(self,f,state=True):
        if self.region!=state:
            if state:
                self.emit_command(f,"G36");
                self.region=True;
            else:
                self.emit_command(f,"G37");
                self.region=False;
           
    def emit_region(self,f,poly):
        self.ensure_region(f,True);
        points = iter(poly['VERTICIES']);
        first_point = points.next();
        self.move_to(f,first_point);
        for point in points:
            self.draw_to(f,point);
        self.draw_to(f,first_point);
        
    def write_gerber_select_aperture(self,f,c):
        req_aperture_code = self.aperture_diameters[c];
        if self.current_aperture_code!=req_aperture_code:
            self.emit_command(f,'D%d' % req_aperture_code);
            self.current_aperture_code = req_aperture_code;

    def reset_gerber_state(self,f):
        self.emit_level(f,dark=True);
        self.region=False;
        self.X = -1.0;
        self.Y = -1.0;
        self.current_aperture_diameter = -1;
        self.current_aperture_code = -1;
        self.current_drill_diameter = -1;

    def write_gerber_header(self,f):
        self.emit_parameter(f,"G04","Lancaster University RF PCB");
        self.emit_precision(f);
        self.emit_parameter(f,"MO","MM");
        self.emit_parameter(f,"SR","X1Y1I0J0");
        
        self.reset_gerber_state(f);
                
    def write_gerber_apertures(self,f):
        self.aperture_counter = 10;
        for c in self.circular_apertures:
            self.define_gerber_circular_aperture(f,self.aperture_counter,c);
            self.aperture_counter += 1;
                            
    def write_gerber_track(self,f,poly):
        self.ensure_region(f,False);
        if DXFFile.LINEWIDTH in poly:
            self.write_gerber_select_aperture(f,poly[DXFFile.LINEWIDTH]);
        else:
            self.write_gerber_select_aperture(f,0.0);
            print "Bug! writing a zero-width open line";
        
        points = iter(poly['VERTICES']);
        first_point = points.next();
        self.move_to(f,(first_point[DXFFile.X],first_point[DXFFile.Y]));
        for point in points:
            self.draw_to(f,(point[DXFFile.X],point[DXFFile.Y]));        
        
    def write_gerber_region(self,f,poly):
        self.ensure_region(f,True);
        points = iter(poly['VERTICES']);
        first_point = points.next();
        self.move_to(f,(first_point[DXFFile.X],first_point[DXFFile.Y]));
        for point in points:
            self.draw_to(f,(point[DXFFile.X],point[DXFFile.Y]));        
        self.draw_to(f,(first_point[DXFFile.X],first_point[DXFFile.Y]));
        
    def write_gerber_flash(self,f,c):
        if c[DXFFile.X]==0.0:
            if c[DXFFile.Y]==0.0:
                return
        self.write_gerber_select_aperture(f,c[DXFFile.DIAMETER]);
        self.flash_command(f,(c[DXFFile.X],c[DXFFile.Y]),'D03');
                
    def write_gerber_trailer(self,f):
        self.ensure_region(f,False);
        self.emit_command(f,"M02");
        
    def write_gerber_file(self,fname,dxf,layernames):
        print 'Writing Gerber file %s' % fname;
        
        entities = self.process_dxf_for_writing(dxf,layernames);
        
        print 'File will contain %d regions, %d tracks and %d circles' % (len(entities['Regions']),len(entities['Tracks']),len(entities['Circles']));
        
        if len(entities['Regions'])==0:
            if len(entities['Tracks'])==0:
                if len(entities['Circles'])==0:
                    print "File will be empty: Skipping file %s" % fname;
                    try:
                        os.unlink(fname);
                    except:
                        pass;
                    return
        
        with open(fname,'w') as f:
            self.write_gerber_header(f);
            self.write_gerber_apertures(f);
            
            print "Writing %d Tracks" % (len(entities['Tracks']));
            
            for c in self.circular_apertures:
                for p in entities['Tracks']:
                    if DXFFile.LINEWIDTH in p:
                        if p[DXFFile.LINEWIDTH]==c:
                            self.write_gerber_track(f,p);
                    else:
                        if c==0.0:
                            self.write_gerber_track(f,p);
 
            print "Flashing %d Apertures" % (len(self.circular_apertures));           
            for d in self.circular_apertures:
                for c in self.no_duplicates(sorted(list(entities['Circles']),cmp=self.XthenY)):
                    if d==c[DXFFile.DIAMETER]:
                        self.write_gerber_flash(f,c);
 
            print "Writing %d Regions" % (len(entities['Regions']));     
            for r in entities['Regions']:
                self.write_gerber_region(f,r);
        
            self.write_gerber_trailer(f);
        
    # To do with excellon
    
    def write_excellon_header(self,f):
        print >> f, "%";
        print >> f, "M48";
        print >> f, "METRIC,TZ";
        print >> f, "M71";
        
    def define_excellon_drill_diameter(self,f,n,d):
        dia = self.default_diameter if d==0.0 else d;
        print >> f, "T%02dC%4.3f" % (n,math.ceil(dia*10.0)/10.0);                    
        self.excellon_drill_diameters[d]=n;
        self.excellon_drill_codes[n]=d;
        
    def write_excellon_drills(self,f):
        self.excellon_drill_counter = 1;
        for c in self.circular_apertures:
            if c==0.0:
                continue;
            self.define_excellon_drill_diameter(f,self.excellon_drill_counter,c);
            self.excellon_drill_counter += 1;
        
    def write_excellon_select_drill(self,f,d):
        req_drill_code = self.excellon_drill_diameters[d];
        if self.current_excellon_drill_code!=req_drill_code:
            print >> f, "T%02d" % req_drill_code;
            self.current_excellon_drill_code = req_drill_code;
            
    def write_excellon_cut(self,f,p):
        pass;
        
    def write_excellon_cutout(self,f,p):
        pass;
                
    def write_excellon_drill_point(self,f,c):
        self.write_excellon_select_drill(f,c[DXFFile.DIAMETER]);
        print >> f, GerberWriter.exc_emit_point((c[DXFFile.X],c[DXFFile.Y]));
                
    def write_excellon_trailer(self,f):
        print >> f, "M30";
    
    def write_excellon_file(self,fname,dxf,layernames):
        print 'Writing Excellon file %s' % fname;

        killfile = True;
        entities = self.process_dxf_for_writing(dxf,layernames);
        diameters = sorted(list(self.circular_apertures));
    
        with open(fname,'w') as f:

            self.write_excellon_header(f);
            self.write_excellon_drills(f);
            
            print >> f, "%";
            print >> f, "G05";
                            
            for dia in diameters:
                print "Diameter = %g" % (dia);
                
                if dia==0.0:
                    print "Skipping diameter 0 holes";
                    continue;
                
                print "Processing entries for drill diameter %g" % (dia);
                                
                holes = list(self.no_duplicates(sorted(entities['Circles'],cmp=self.XthenY)));
                
                print "Drilling %d holes\n" % len(holes);
                                
                for circle in holes: 
                    if circle[DXFFile.DIAMETER]==dia:
                        self.write_excellon_drill_point(f,circle);
                                                
              #  print "Making %d cuts\n" % len(entities['Tracks']);
              #  
              #  print >> f, "G01";
              #  
              #  for p in entities['Tracks']:
              #      if DXFFile.LINEWIDTH in p:
              #          if p[DXFFile.LINEWIDTH]==dia:
              #              self.write_excellon_cut(f,p);
              #      else:
              #          raise Exception("Error: trying to cut a slot with zero cutter width");
              #    
              #  print "Making %d cut-outs\n" % (len(entities['Regions']));
              #  
              #  print >> f, "G01";
              #
              #  for r in entities['Regions']:        
              #      print r;        
              #      if DXFFile.LINEWIDTH in r:
              #          print "Cut-out has width %g" % (r[DXFFile.LINEWIDTH]);
              #          if r[DXFFile.LINEWIDTH]==dia:
              #              self.write_excellon_cutout(f,r);
              #      else:
              #          raise Exception("Error: trying to cut a cut-out with zero cutter width");
              
            self.write_excellon_trailer(f);
                                                                    
    def process_cam(self,dxf,camname=None):
        
        self.clear_aperture_cache();
        self.measure_dxf_file(dxf);

        # Pick a sensible output filename
                
        if camname == None:
            camname = dxf.filename;
            
        self.cam_base = os.path.splitext(camname)[0];
                
        # For each layer, produce a Gerber or Excellon file
        
        print "\n\nProcessing Gerber files\n";
        
        for extension in self.gerber_layers:
            ofname = self.cam_base+extension;   
            print "Writing data of type %s to file %s" % (self.gerber_layers[extension][0],ofname);
            self.write_gerber_file(ofname,dxf,self.gerber_layers[extension]);
            print "";
            
        print "\n\nProcessing Excellon files\n";
        
        # For each layer, produce a Gerber or Excellon file
        
        for extension in self.excellon_layers:
            ofname = self.cam_base+extension;   
            print "Writing data of type %s to file %s" % (self.excellon_layers[extension][0],ofname);
            self.write_excellon_file(ofname,dxf,self.excellon_layers[extension]);
            print "";
                
        print "\n\nDone\n";

# The main program
                          
if __name__=="__main__":

    for f in glob.glob('G:\\resonator_board\\*.dxf'):
        
        print 'Processing file %s' % f;
             
        d = DXFFile(f);
        g = GerberWriter();
        
        g.process_cam(d);
        
        del g;
        del d;
